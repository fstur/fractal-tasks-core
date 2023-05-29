"""
This script generates JSON schemas for task arguments afresh, and writes them
to files starting like `fractal_tasks_core/__args__create_ome_zarr__.json`
"""
import argparse
import json
from importlib import import_module
from pathlib import Path

import fractal_tasks_core


FRACTAL_TASKS_CORE_DIR = Path(fractal_tasks_core.__file__).parent


def get_task_list_from_manifest() -> list[dict]:
    with (FRACTAL_TASKS_CORE_DIR / "__FRACTAL_MANIFEST__.json").open("r") as f:
        manifest = json.load(f)
    task_list = manifest["task_list"]
    return task_list


def create_schema_for_single_task(task: dict):
    executable = task["executable"]
    if not executable.endswith(".py"):
        raise ValueError(f"Invalid {executable=}")
    module_name = executable[:-3]
    module = import_module(f"fractal_tasks_core.{module_name}")
    TaskArguments = getattr(module, "TaskArguments")
    schema = TaskArguments.schema()
    return schema, module_name


if __name__ == "__main__":

    parser_main = argparse.ArgumentParser(
        description="Create/update task-arguments JSON schemas"
    )
    subparsers_main = parser_main.add_subparsers(
        title="Commands:", dest="command", required=True
    )
    parser_check = subparsers_main.add_parser(
        "check",
        description="Check that existing files are up-to-date",
        allow_abbrev=False,
    )
    parser_check = subparsers_main.add_parser(
        "new",
        description="Write new JSON schemas to files",
        allow_abbrev=False,
    )

    args = parser_main.parse_args()
    command = args.command

    with (FRACTAL_TASKS_CORE_DIR / "__FRACTAL_MANIFEST__.json").open("r") as f:
        manifest = json.load(f)
    task_list = manifest["task_list"]
    manifest["has_args_schema"] = True
    manifest["args_schema_version"] = "pydantic_v1"

    for ind, task in enumerate(task_list):
        print(f"Now handling {task['executable']}")
        try:
            schema, module_name = create_schema_for_single_task(task)
        except AttributeError:
            print(f"Skip {module_name}, due to AttributeError")
            print()
            continue

        if command == "check":
            current_schema = task["args_schema"]
            if not current_schema == schema:
                raise ValueError("Schemas are different.")
            print("Schema in manifest is up-to-date.")
            print()
        elif command == "new":
            manifest["task_list"][ind]["args_schema"] = schema
            print("Schema added to manifest")
            print()

    with (FRACTAL_TASKS_CORE_DIR / "__FRACTAL_MANIFEST__.json").open("w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
