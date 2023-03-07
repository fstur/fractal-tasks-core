"""
Copyright 2022 (C)
    Friedrich Miescher Institute for Biomedical Research and
    University of Zurich

    Original authors:
    Tommaso Comparin <tommaso.comparin@exact-lab.it>
    Marco Franzon <marco.franzon@exact-lab.it>
    Joel Lüthi  <joel.luethi@fmi.ch>

    This file is part of Fractal and was originally developed by eXact lab
    S.r.l.  <exact-lab.it> under contract with Liberali Lab from the Friedrich
    Miescher Institute for Biomedical Research and Pelkmans Lab from the
    University of Zurich.

Image segmentation via Cellpose library
"""
import json
import logging
import os
import time
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Dict
from typing import Optional
from typing import Sequence
from typing import Tuple

import anndata as ad
import dask.array as da
import numpy as np
import pandas as pd
import zarr
from anndata.experimental import write_elem
from cellpose import models
from cellpose.core import use_gpu

import fractal_tasks_core
from fractal_tasks_core.lib_channels import ChannelNotFoundError
from fractal_tasks_core.lib_channels import get_channel_from_image_zarr
from fractal_tasks_core.lib_pyramid_creation import build_pyramid
from fractal_tasks_core.lib_regions_of_interest import (
    array_to_bounding_box_table,
)
from fractal_tasks_core.lib_regions_of_interest import (
    convert_ROI_table_to_indices,
)
from fractal_tasks_core.lib_remove_FOV_overlaps import (
    get_overlapping_pairs_3D,
)
from fractal_tasks_core.lib_zattrs_utils import extract_zyx_pixel_sizes
from fractal_tasks_core.lib_zattrs_utils import rescale_datasets

logger = logging.getLogger(__name__)

__OME_NGFF_VERSION__ = fractal_tasks_core.__OME_NGFF_VERSION__
ModelInCellposeZoo = Enum(
    "ModelInCellposeZoo",
    ((value, value) for value in models.MODEL_NAMES),
    type=str,
)


def preprocess_cellpose_input(
    *,
    image_array: np.ndarray,
    use_masks: bool = False,
    region: Optional[Tuple[slice]] = None,
    current_label_path: Optional[str] = None,
    primary_label_path: Optional[str] = None,
    ROI_table_obs: Optional[pd.DataFrame] = None,
    index_ROI: Optional[int] = None,
) -> Tuple[np.ndarray, Optional[np.ndarray], Optional[np.ndarray]]:
    """
    Preprocess a four-dimensional cellpose input, either in a trivial way (if
    `use_masks=False`) or by setting the background to zero (if
    `use_masks=True`).

    All arrays correspond to a given region, as defined in indices (see below).

    FIXME: improve naming of variables

    FIXME: improve docstring

    FIXME: add organoid/nuclei example

    Arguments:
        image_array: 4D image array - TBD
        use_masks: TBD
        region: TBD
        current_mask_array:
            the current state of the cellpose output (part of it will be
            overwritten, part of it will need to be restored afterwards)
        primary_label_array:
            label array that is used for masking (e.g. organoid bounding-box
            ROIs)
        label_value:
            The value to be matched, in `primary_label_array` (e.g. the
            organoid label)
    """
    if not image_array.ndim == 4:
        raise ValueError(
            "preprocess_cellpose_input requires a four-dimensional "
            f"image_array argument, but {image_array.shape=}"
        )
    if use_masks:
        if None in (
            region,
            current_label_path,
            primary_label_path,
            ROI_table_obs,
            index_ROI,
        ):
            raise ValueError(
                f"preprocess_cellpose_input called with {use_masks=} but "
                f"{primary_label_path=}, {current_label_path=}, "
                f"{region=} and {ROI_table_obs=} and {index_ROI=}."
            )
        # Check that ROI_table has the obs.label column
        if "label" not in ROI_table_obs.columns:
            raise ValueError(
                'In preprocess_cellpose_input, "label" '
                f" missing in {ROI_table_obs.columns=}"
            )
        label_value = int(ROI_table_obs.label[index_ROI])

        # Load primary label array
        primary_label_array = da.from_zarr(primary_label_path)[
            region
        ].compute()  # noqa
        if primary_label_array.shape != image_array.shape[1:]:
            raise ValueError(
                f"In preprocess_cellpose_input, {image_array.shape=} but "
                f"{primary_label_array.shape=}."
            )
        # Load current label array
        current_label_array = da.from_zarr(current_label_path)[
            region
        ].compute()  # noqa

        # Compute background mask
        background_3D = primary_label_array != label_value
        if (primary_label_array == label_value).sum() == 0:
            raise ValueError

        # Set image background to zero
        background_4D = np.expand_dims(background_3D, axis=0)
        image_array[background_4D] = 0

        return (image_array, background_3D, current_label_array)
    else:
        return (image_array, None, None)


def postprocess_cellpose_output(
    *,
    use_masks: bool = False,
    new_label_array: Optional[np.ndarray] = None,
    old_label_array: Optional[np.ndarray] = None,
    background_3D: Optional[np.ndarray] = None,
) -> np.ndarray:
    """
    FIXME docstring

    Main goal of this function, for the moment, is to restore a the old
    background labels
    """
    if use_masks:
        if not all(
            (
                new_label_array.ndim == 3,
                old_label_array.ndim == 3,
                old_label_array.shape == new_label_array.shape,
                background_3D.shape == new_label_array.shape,
            )
        ):
            raise ValueError(
                "In postprocess_cellpose_output:\n"
                f"{use_masks=}\n"
                f"{old_label_array.shape=}\n"
                f"{new_label_array.shape=}\n"
                f"{background_3D.shape=}"
            )
        new_label_array[background_3D] = old_label_array[background_3D]
        return new_label_array
    else:
        return new_label_array


def segment_FOV(
    x: np.ndarray,
    model: models.CellposeModel = None,
    do_3D: bool = True,
    channels=[0, 0],
    anisotropy: Optional[float] = None,
    diameter: float = 30.0,
    cellprob_threshold: float = 0.0,
    flow_threshold: float = 0.4,
    label_dtype: Optional[np.dtype] = None,
    augment: bool = False,
    net_avg: bool = False,
    min_size: int = 15,
) -> np.ndarray:
    """
    Internal function that runs Cellpose segmentation for a single ROI.

    :param x: 4D numpy array
    :param model: An instance of models.CellposeModel
    :param do_3D: If true, cellpose runs in 3D mode: runs on xy, xz & yz
                  planes, then averages the flows.
    :param channels: Which channels to use. If only one channel is provided,
                     [0, 0] should be used. If two channels are provided
                     (the first dimension of x has lenth of 2), [[1, 2]]
                     should be used (x[0, :, :, :] contains the membrane
                     channel first & x[1, :, :, :] the nuclear channel).
    :param anisotropy: Set anisotropy rescaling factor for Z dimension
    :param diameter: Expected object diameter in pixels for cellpose
    :param cellprob_threshold: Cellpose model parameter
    :param flow_threshold: Cellpose model parameter
    :param label_dtype: Label images are cast into this np.dtype
    :param augment: Whether to use cellpose augmentation to tile images
                    with overlap
    :param net_avg: Whether to use cellpose net averaging to run the 4 built-in
                    networks (useful for nuclei, cyto & cyto2, not sure it
                    works for the others)
    :param min_size: Minimum size of the segmented objects
    """

    # Write some debugging info
    logger.info(
        "[segment_FOV] START |"
        f" x: {type(x)}, {x.shape} |"
        f" {do_3D=} |"
        f" {model.diam_mean=} |"
        f" {diameter=} |"
        f" {flow_threshold=}"
    )

    # Actual labeling
    t0 = time.perf_counter()
    mask, _, _ = model.eval(
        x,
        channels=channels,
        do_3D=do_3D,
        net_avg=net_avg,
        augment=augment,
        diameter=diameter,
        anisotropy=anisotropy,
        cellprob_threshold=cellprob_threshold,
        flow_threshold=flow_threshold,
        min_size=min_size,
    )

    if mask.ndim == 2:
        # If we get a 2D image, we still return it as a 3D array
        mask = np.expand_dims(mask, axis=0)
    t1 = time.perf_counter()

    # Write some debugging info
    logger.info(
        "[segment_FOV] END   |"
        f" Elapsed: {t1-t0:.3f} s |"
        f" {mask.shape=},"
        f" {mask.dtype=} (then {label_dtype}),"
        f" {np.max(mask)=} |"
        f" {model.diam_mean=} |"
        f" {diameter=} |"
        f" {flow_threshold=}"
    )

    return mask.astype(label_dtype)


def cellpose_segmentation(
    *,
    # Fractal arguments
    input_paths: Sequence[str],
    output_path: str,
    component: str,
    metadata: Dict[str, Any],
    # Task-specific arguments
    level: int,
    wavelength_id: Optional[str] = None,
    channel_label: Optional[str] = None,
    wavelength_id_c2: Optional[str] = None,
    channel_label_c2: Optional[str] = None,
    relabeling: bool = True,
    anisotropy: Optional[float] = None,
    diameter_level0: float = 30.0,
    cellprob_threshold: float = 0.0,
    flow_threshold: float = 0.4,
    ROI_table_name: str = "FOV_ROI_table",
    bounding_box_ROI_table_name: Optional[str] = None,
    output_label_name: Optional[str] = None,
    model_type: ModelInCellposeZoo = "cyto2",
    pretrained_model: Optional[str] = None,
    min_size: int = 15,
    augment: bool = False,
    net_avg: bool = False,
    use_masks: bool = False,
    primary_label_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run cellpose segmentation on the ROIs of a single OME-NGFF image

    Full documentation for all arguments is still TBD, especially because some
    of them are standard arguments for Fractal tasks that should be documented
    in a standard way. Here are some examples of valid arguments::

        input_paths = ["/some/path/"]
        output_path = "/some/path/"
        component = "some_plate.zarr/B/03/0"
        metadata = {"num_levels": 4, "coarsening_xy": 2}

    :param input_paths: TBD (default arg for Fractal tasks)
    :param output_path: TBD (default arg for Fractal tasks)
    :param metadata: TBD (default arg for Fractal tasks)
    :param component: TBD (default arg for Fractal tasks)
    :param level: Pyramid level of the image to be segmented.
    :param wavelength_id: Identifier of a channel based on the
                          wavelength (e.g. ``A01_C01``). If not ``None``, then
                          ``channel_label` must be ``None``.
    :param channel_label: Identifier of a channel based on its label (e.g.
                          ``DAPI``). If not ``None``, then ``wavelength_id``
                          must be ``None``.
    :param wavelength_id_c2: Identifier of a second channel in the same format
                          as the first wavelength_id. If specified, cellpose
                          runs in dual channel mode.
                          For dual channel segmentation of cells, the first
                          channel should contain the membrane marker,
                          the second channel should contain the nuclear marker.
    :param channel_label_c2: Identifier of a second channel in the same
                          format as the first wavelength_id. If specified,
                          cellpose runs in dual channel mode.
                          For dual channel segmentation of cells,
                          the first channel should contain the membrane marker,
                          the second channel should contain the nuclear marker.
    :param relabeling: If ``True``, apply relabeling so that label values are
                       unique across ROIs.
    :param anisotropy: Ratio of the pixel sizes along Z and XY axis (ignored if
                       the image is not three-dimensional). If `None`, it is
                       inferred from the OME-NGFF metadata.
    :param diameter_level0: Initial diameter to be passed to
                            ``CellposeModel.eval`` method (after rescaling from
                            full-resolution to ``level``).
    :param ROI_table_name: name of the table that contains ROIs to which the
                           task applies Cellpose segmentation
    :param bounding_box_ROI_table_name: TBD
    :param output_label_name: TBD
    :param cellprob_threshold: Parameter of ``CellposeModel.eval`` method.
    :param flow_threshold: Parameter of ``CellposeModel.eval`` method.
    :param model_type: Parameter of ``CellposeModel`` class.
    :param pretrained_model: Parameter of ``CellposeModel`` class (takes
                             precedence over ``model_type``).
    :param min_size: Minimum size of the segmented objects (in pixels).
                     Use -1 to turn off the size filter
    :param agument: Whether to use cellpose augmentation to tile images
                    with overlap
    :param net_avg: Whether to use cellpose net averaging to run the 4 built-in
                    networks (useful for nuclei, cyto & cyto2, not sure it
                    works for the others)
    :param primary_label_name: FIXME docstring
    :param use_masks: FIXME docstring
    """

    # Set input path
    if len(input_paths) > 1:
        raise NotImplementedError
    in_path = Path(input_paths[0])
    zarrurl = (in_path.resolve() / component).as_posix() + "/"
    logger.info(f"{zarrurl=}")

    # Preliminary check
    if (channel_label is None and wavelength_id is None) or (
        channel_label and wavelength_id
    ):
        raise ValueError(
            f"One and only one of {channel_label=} and "
            f"{wavelength_id=} arguments must be provided"
        )

    # Read useful parameters from metadata
    num_levels = metadata["num_levels"]
    coarsening_xy = metadata["coarsening_xy"]

    plate, well = component.split(".zarr/")

    # Find channel index
    try:
        channel = get_channel_from_image_zarr(
            image_zarr_path=zarrurl,
            wavelength_id=wavelength_id,
            label=channel_label,
        )
    except ChannelNotFoundError as e:
        logger.warning(
            "Channel not found, exit from the task.\n"
            f"Original error: {str(e)}"
        )
        return {}
    ind_channel = channel["index"]

    # Find channel index for second channel, if one is provided
    if wavelength_id_c2 or channel_label_c2:
        try:
            channel_c2 = get_channel_from_image_zarr(
                image_zarr_path=zarrurl,
                wavelength_id=wavelength_id_c2,
                label=channel_label_c2,
            )
        except ChannelNotFoundError as e:
            logger.warning(
                f"Second channel with wavelength_id_c2:{wavelength_id_c2} and "
                f"channel_label_c2: {channel_label_c2} not found, exit "
                "from the task.\n"
                f"Original error: {str(e)}"
            )
            return {}
        ind_channel_c2 = channel_c2["index"]

    # Set channel label
    if output_label_name is None:
        try:
            channel_label = channel["label"]
            output_label_name = f"label_{channel_label}"
        except (KeyError, IndexError):
            output_label_name = f"label_{ind_channel}"

    # Load ZYX data
    data_zyx = da.from_zarr(f"{zarrurl}{level}")[ind_channel]
    logger.info(f"{data_zyx.shape=}")
    if wavelength_id_c2 or channel_label_c2:
        data_zyx_c2 = da.from_zarr(f"{zarrurl}{level}")[ind_channel_c2]
        logger.info(f"Second channel: {data_zyx_c2.shape=}")

    # Read ROI table
    ROI_table = ad.read_zarr(f"{zarrurl}tables/{ROI_table_name}")

    # Read pixel sizes from zattrs file
    full_res_pxl_sizes_zyx = extract_zyx_pixel_sizes(
        f"{zarrurl}.zattrs", level=0
    )

    actual_res_pxl_sizes_zyx = extract_zyx_pixel_sizes(
        f"{zarrurl}.zattrs", level=level
    )
    # Create list of indices for 3D FOVs spanning the entire Z direction
    # FIXME: set reset_origin correctly
    if ROI_table_name in ["FOV_ROI_table", "well_ROI_table"]:
        reset_origin = True
    else:
        reset_origin = False
    list_indices = convert_ROI_table_to_indices(
        ROI_table,
        level=level,
        coarsening_xy=coarsening_xy,
        full_res_pxl_sizes_zyx=full_res_pxl_sizes_zyx,
        reset_origin=reset_origin,
    )

    # Extract image size from FOV-ROI indices
    # Note: this works at level=0, where FOVs should all be of the exact same
    #       size (in pixels)
    FOV_ROI_table = ad.read_zarr(f"{zarrurl}tables/FOV_ROI_table")
    logger.info(f"{reset_origin=}")
    list_FOV_indices_level0 = convert_ROI_table_to_indices(
        FOV_ROI_table,
        level=0,
        full_res_pxl_sizes_zyx=full_res_pxl_sizes_zyx,
    )
    ref_img_size = None
    for indices in list_FOV_indices_level0:
        img_size = (indices[3] - indices[2], indices[5] - indices[4])
        if ref_img_size is None:
            ref_img_size = img_size
        else:
            if img_size != ref_img_size:
                raise Exception(
                    "ERROR: inconsistent image sizes in "
                    f"{list_FOV_indices_level0=}"
                )
    img_size_y, img_size_x = img_size[:]

    # Select 2D/3D behavior and set some parameters
    do_3D = data_zyx.shape[0] > 1
    if do_3D:
        if anisotropy is None:
            # Read pixel sizes from zattrs file
            pxl_zyx = extract_zyx_pixel_sizes(zarrurl + ".zattrs", level=level)
            pixel_size_z, pixel_size_y, pixel_size_x = pxl_zyx[:]
            logger.info(f"{pxl_zyx=}")
            if not np.allclose(pixel_size_x, pixel_size_y):
                raise Exception(
                    "ERROR: XY anisotropy detected"
                    f"pixel_size_x={pixel_size_x}"
                    f"pixel_size_y={pixel_size_y}"
                )
            anisotropy = pixel_size_z / pixel_size_x

    # Prelminary checks on Cellpose model
    if pretrained_model is None:
        if model_type not in models.MODEL_NAMES:
            raise ValueError(f"ERROR model_type={model_type} is not allowed.")
    else:
        if not os.path.exists(pretrained_model):
            raise ValueError(f"{pretrained_model=} does not exist.")

    # Load zattrs file
    zattrs_file = f"{zarrurl}.zattrs"
    with open(zattrs_file, "r") as jsonfile:
        zattrs = json.load(jsonfile)

    # Preliminary checks on multiscales
    multiscales = zattrs["multiscales"]
    if len(multiscales) > 1:
        raise NotImplementedError(
            f"Found {len(multiscales)} multiscales, "
            "but only one is currently supported."
        )
    if "coordinateTransformations" in multiscales[0].keys():
        raise NotImplementedError(
            "global coordinateTransformations at the multiscales "
            "level are not currently supported"
        )

    # Rescale datasets (only relevant for level>0)
    new_datasets = rescale_datasets(
        datasets=multiscales[0]["datasets"],
        coarsening_xy=coarsening_xy,
        reference_level=level,
    )

    # Write zattrs for labels and for specific label
    new_labels = [output_label_name]
    try:
        with open(f"{zarrurl}labels/.zattrs", "r") as f_zattrs:
            existing_labels = json.load(f_zattrs)["labels"]
    except FileNotFoundError:
        existing_labels = []
    intersection = set(new_labels) & set(existing_labels)
    logger.info(f"{new_labels=}")
    logger.info(f"{existing_labels=}")
    if intersection:
        raise RuntimeError(
            f"Labels {intersection} already exist but are also part of outputs"
        )
    labels_group = zarr.group(f"{zarrurl}labels")
    labels_group.attrs["labels"] = existing_labels + new_labels

    label_group = labels_group.create_group(output_label_name)
    label_group.attrs["image-label"] = {"version": __OME_NGFF_VERSION__}
    label_group.attrs["multiscales"] = [
        {
            "name": output_label_name,
            "version": __OME_NGFF_VERSION__,
            "axes": [
                ax for ax in multiscales[0]["axes"] if ax["type"] != "channel"
            ],
            "datasets": new_datasets,
        }
    ]

    # Open new zarr group for mask 0-th level
    zarr.group(f"{zarrurl}/labels")
    zarr.group(f"{zarrurl}/labels/{output_label_name}")
    logger.info(f"Output label path: {zarrurl}labels/{output_label_name}/0")
    store = zarr.storage.FSStore(f"{zarrurl}labels/{output_label_name}/0")
    label_dtype = np.uint32
    mask_zarr = zarr.create(
        shape=data_zyx.shape,
        chunks=data_zyx.chunksize,
        dtype=label_dtype,
        store=store,
        overwrite=False,
        dimension_separator="/",
    )

    logger.info(
        f"mask will have shape {data_zyx.shape} "
        f"and chunks {data_zyx.chunks}"
    )

    # Initialize cellpose
    gpu = use_gpu()
    if pretrained_model:
        model = models.CellposeModel(
            gpu=gpu, pretrained_model=pretrained_model
        )
    else:
        model = models.CellposeModel(gpu=gpu, model_type=model_type)

    # Initialize other things
    logger.info(f"Start cellpose_segmentation task for {zarrurl}")
    logger.info(f"relabeling: {relabeling}")
    logger.info(f"do_3D: {do_3D}")
    logger.info(f"use_gpu: {gpu}")
    logger.info(f"level: {level}")
    logger.info(f"model_type: {model_type}")
    logger.info(f"pretrained_model: {pretrained_model}")
    logger.info(f"anisotropy: {anisotropy}")
    logger.info("Total well shape/chunks:")
    logger.info(f"{data_zyx.shape}")
    logger.info(f"{data_zyx.chunks}")
    if wavelength_id_c2 or channel_label_c2:
        logger.info("Dual channel input for cellpose model")
        logger.info(f"{data_zyx_c2.shape}")
        logger.info(f"{data_zyx_c2.chunks}")

    # Counters for relabeling
    if relabeling:
        num_labels_tot = 0

    # Iterate over ROIs
    num_ROIs = len(list_indices)

    if bounding_box_ROI_table_name:
        bbox_dataframe_list = []

    logger.info(f"Now starting loop over {num_ROIs} ROIs")
    for i_ROI, indices in enumerate(list_indices):
        # Define region
        s_z, e_z, s_y, e_y, s_x, e_x = indices[:]
        region = (
            slice(s_z, e_z),
            slice(s_y, e_y),
            slice(s_x, e_x),
        )
        logger.info(f"Now processing ROI {i_ROI+1}/{num_ROIs}")
        # Execute cellpose segmentation
        if wavelength_id_c2 or channel_label_c2:
            # Dual channel mode, first channel is the membrane channel
            img_np = np.zeros((2, *data_zyx[s_z:e_z, s_y:e_y, s_x:e_x].shape))
            img_np[0, :, :, :] = data_zyx[s_z:e_z, s_y:e_y, s_x:e_x].compute()
            img_np[1, :, :, :] = data_zyx_c2[
                s_z:e_z, s_y:e_y, s_x:e_x
            ].compute()
            channels = [1, 2]
        else:
            img_np = np.expand_dims(
                data_zyx[s_z:e_z, s_y:e_y, s_x:e_x].compute(), axis=0
            )
            channels = [0, 0]

        img_np, background_3D, current_label = preprocess_cellpose_input(
            image_array=img_np,
            use_masks=use_masks,
            region=region,
            primary_label_path=f"{zarrurl}labels/{primary_label_name}/0",  # FIXME: which level? # noqa
            current_label_path=f"{zarrurl}labels/{output_label_name}/0",  # FIXME: which level? # noqa
            ROI_table_obs=ROI_table.obs,
            index_ROI=i_ROI,
        )

        new_mask = segment_FOV(
            img_np,
            model=model,
            channels=channels,
            do_3D=do_3D,
            anisotropy=anisotropy,
            label_dtype=label_dtype,
            diameter=diameter_level0 / coarsening_xy**level,
            cellprob_threshold=cellprob_threshold,
            flow_threshold=flow_threshold,
            min_size=min_size,
            augment=augment,
            net_avg=net_avg,
        )

        new_mask = postprocess_cellpose_output(
            use_masks=use_masks,
            new_label_array=new_mask,
            old_label_array=current_label,
            background_3D=background_3D,
        )

        # Shift labels and update relabeling counters
        if relabeling:
            num_labels_fov = np.max(new_mask)
            new_mask[new_mask > 0] += num_labels_tot
            num_labels_tot += num_labels_fov

            # Write some logs
            logger.info(
                f"FOV ROI {indices}, "
                f"{num_labels_fov=}, "
                f"{num_labels_tot=}"
            )

            # Check that total number of labels is under control
            if num_labels_tot > np.iinfo(label_dtype).max:
                raise Exception(
                    "ERROR in re-labeling:"
                    f"Reached {num_labels_tot} labels, "
                    f"but dtype={label_dtype}"
                )

        if bounding_box_ROI_table_name:

            bbox_df = array_to_bounding_box_table(
                new_mask, actual_res_pxl_sizes_zyx
            )

            bbox_dataframe_list.append(bbox_df)

            overlap_list = []
            for df in bbox_dataframe_list:
                overlap_list.extend(
                    get_overlapping_pairs_3D(df, full_res_pxl_sizes_zyx)
                )
            if len(overlap_list) > 0:
                logger.warning(
                    f"{len(overlap_list)} bounding-box pairs overlap"
                )

        # Compute and store 0-th level to disk
        da.array(new_mask).to_zarr(
            url=mask_zarr,
            region=region,
            compute=True,
        )

    logger.info(
        f"End cellpose_segmentation task for {zarrurl}, "
        "now building pyramids."
    )

    # Starting from on-disk highest-resolution data, build and write to disk a
    # pyramid of coarser levels
    build_pyramid(
        zarrurl=f"{zarrurl}labels/{output_label_name}",
        overwrite=False,
        num_levels=num_levels,
        coarsening_xy=coarsening_xy,
        chunksize=data_zyx.chunksize,
        aggregation_function=np.max,
    )

    logger.info("End building pyramids")

    if bounding_box_ROI_table_name:
        # Concatenate all FOV dataframes
        df_well = pd.concat(bbox_dataframe_list, axis=0, ignore_index=True)
        df_well.index = df_well.index.astype(str)
        # Extract labels and drop them from df_well
        labels = pd.DataFrame(df_well["label"].astype(str))
        df_well.drop(labels=["label"], axis=1, inplace=True)
        # Convert all to float (warning: some would be int, in principle)
        bbox_dtype = np.float32
        df_well = df_well.astype(bbox_dtype)
        # Convert to anndata
        bbox_table = ad.AnnData(df_well, dtype=bbox_dtype)
        bbox_table.obs = labels
        # Write to zarr group
        group_tables = zarr.group(f"{in_path}/{component}/tables/")
        write_elem(group_tables, bounding_box_ROI_table_name, bbox_table)
        logger.info(
            "Bounding box ROI table written to "
            f"{in_path}/{component}/tables/{bounding_box_ROI_table_name}"
        )

    return {}


if __name__ == "__main__":

    from pydantic import BaseModel
    from pydantic import Extra
    from fractal_tasks_core._utils import run_fractal_task

    class TaskArguments(BaseModel, extra=Extra.forbid):
        # Fractal arguments
        input_paths: Sequence[str]
        output_path: str
        component: str
        metadata: Dict[str, Any]
        # Task-specific arguments
        channel_label: Optional[str]
        wavelength_id: Optional[str]
        channel_label_c2: Optional[str]
        channel_label_c2: Optional[str]
        level: int
        relabeling: bool = True
        anisotropy: Optional[float] = None
        diameter_level0: Optional[float]
        cellprob_threshold: Optional[float]
        flow_threshold: Optional[float]
        ROI_table_name: Optional[str]
        bounding_box_ROI_table_name: Optional[str]
        output_label_name: Optional[str]
        model_type: Optional[ModelInCellposeZoo]
        pretrained_model: Optional[str]
        min_size: Optional[int]
        augment: Optional[bool]
        net_avg: Optional[bool]
        primary_label_name: Optional[str]
        use_masks: Optional[bool]

    run_fractal_task(
        task_function=cellpose_segmentation,
        TaskArgsModel=TaskArguments,
        logger_name=logger.name,
    )
