"""
Copyright 2022 (C)
    Friedrich Miescher Institute for Biomedical Research and
    University of Zurich

    Original authors:
    Tommaso Comparin <tommaso.comparin@exact-lab.it>
    Marco Franzon <marco.franzon@exact-lab.it>

    This file is part of Fractal and was originally developed by eXact lab
    S.r.l.  <exact-lab.it> under contract with Liberali Lab from the Friedrich
    Miescher Institute for Biomedical Research and Pelkmans Lab from the
    University of Zurich.

Task that writes image data to an existing OME-NGFF zarr array
"""
import logging
import os
import re
from glob import glob
from pathlib import Path
from typing import Any
from typing import Dict
from typing import Sequence

import dask.array as da
import zarr
from anndata import read_zarr
from dask.array.image import imread

from fractal_tasks_core.lib_pyramid_creation import build_pyramid
from fractal_tasks_core.lib_regions_of_interest import (
    convert_ROI_table_to_indices,
)
from fractal_tasks_core.lib_zattrs_utils import extract_zyx_pixel_sizes

logger = logging.getLogger(__name__)


def sort_fun(s: str):
    """
    sort_fun takes a string (filename of a yokogawa images),
    extract site and z-index metadata and returns them as a list.

    :param s: filename
    """

    site = re.findall(r"F(.*)L", s)[0]
    zind = re.findall(r"Z(.*)C", s)[0]
    return [site, zind]


def yokogawa_to_zarr(
    *,
    input_paths: Sequence[Path],
    output_path: Path,
    component: str = None,
    metadata: Dict[str, Any] = None,
    delete_input=False,
):
    """
    Convert Yokogawa output (png, tif) to zarr file

    Example arguments:
      input_paths[0] = /tmp/output/*.zarr  (Path)
      output_path = /tmp/output/*.zarr      (Path)
      metadata = {"channel_list": [...], "num_levels": ..., }
      component = plate.zarr/B/03/0/

    :param input_paths: TBD
    :param output_path: TBD
    :param component: TBD
    :param metadata: TBD
    :param delete_input: TBD
    """

    # Preliminary checks
    if len(input_paths) > 1:
        raise NotImplementedError

    chl_list = metadata["channel_list"]
    original_path_list = metadata["original_paths"]
    in_path = Path(original_path_list[0]).parent
    ext = Path(original_path_list[0]).name
    num_levels = metadata["num_levels"]
    coarsening_xy = metadata["coarsening_xy"]

    # Define well
    component_split = component.split("/")
    well_row = component_split[1]
    well_column = component_split[2]
    well_ID = well_row + well_column

    # Read useful information from ROI table and .zattrs
    zarrurl = input_paths[0].parent.as_posix() + f"/{component}"
    adata = read_zarr(f"{zarrurl}/tables/FOV_ROI_table")
    pxl_size = extract_zyx_pixel_sizes(f"{zarrurl}/.zattrs")
    fov_indices = convert_ROI_table_to_indices(
        adata, full_res_pxl_sizes_zyx=pxl_size
    )
    adata_well = read_zarr(f"{zarrurl}/tables/well_ROI_table")
    well_indices = convert_ROI_table_to_indices(
        adata_well, full_res_pxl_sizes_zyx=pxl_size
    )
    if len(well_indices) > 1:
        raise Exception(f"Something wrong with {well_indices=}")

    # FIXME: Put back the choice of columns by name? Not here..

    max_z = well_indices[0][1]
    max_y = well_indices[0][3]
    max_x = well_indices[0][5]

    # Load a single image, to retrieve useful information
    sample = imread(glob(f"{in_path}/*_{well_ID}_*{ext}")[0])

    # Initialize zarr
    chunksize = (1, 1, sample.shape[1], sample.shape[2])
    canvas_zarr = zarr.create(
        shape=(len(chl_list), max_z, max_y, max_x),
        chunks=chunksize,
        dtype=sample.dtype,
        store=da.core.get_mapper(zarrurl + "/0"),
        overwrite=False,
        dimension_separator="/",
    )

    # Loop over channels
    for i_c, chl in enumerate(chl_list):
        A, C = chl.split("_")

        glob_path = f"{in_path}/*_{well_ID}_*{A}*{C}{ext}"
        logger.info(f"glob path: {glob_path}")
        filenames = sorted(glob(glob_path), key=sort_fun)
        if len(filenames) == 0:
            raise Exception(
                "Error in yokogawa_to_zarr: len(filenames)=0.\n"
                f"  in_path: {in_path}\n"
                f"  ext: {ext}\n"
                f"  well_ID: {well_ID}\n"
                f"  channel: {chl},\n"
                f"  glob_path: {glob_path}"
            )
        # Loop over 3D FOV ROIs
        for indices in fov_indices:
            s_z, e_z, s_y, e_y, s_x, e_x = indices[:]
            region = (
                slice(i_c, i_c + 1),
                slice(s_z, e_z),
                slice(s_y, e_y),
                slice(s_x, e_x),
            )
            FOV_3D = da.concatenate(
                [imread(img) for img in filenames[:e_z]],
            )
            FOV_4D = da.expand_dims(FOV_3D, axis=0)
            filenames = filenames[e_z:]
            da.array(FOV_4D).to_zarr(
                url=canvas_zarr,
                region=region,
                compute=True,
            )

    # Starting from on-disk highest-resolution data, build and write to disk a
    # pyramid of coarser levels
    build_pyramid(
        zarrurl=zarrurl,
        overwrite=False,
        num_levels=num_levels,
        coarsening_xy=coarsening_xy,
        chunksize=chunksize,
    )

    # Delete images (optional)
    if delete_input:
        for f in filenames:
            try:
                os.remove(f)
            except OSError as e:
                logging.info("Error: %s : %s" % (f, e.strerror))

    return {}


if __name__ == "__main__":
    from pydantic import BaseModel
    from fractal_tasks_core._utils import run_fractal_task

    class TaskArguments(BaseModel):
        input_paths: Sequence[Path]
        output_path: Path
        delete_input = False
        metadata: Dict[str, Any] = None
        component: str = None

    run_fractal_task(
        task_function=yokogawa_to_zarr, TaskArgsModel=TaskArguments
    )
