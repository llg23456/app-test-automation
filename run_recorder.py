import argparse
import datetime
import os
import time

from scripts.utils import print_with_color

arg_desc = "AppAgent - exploration phase"
parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=arg_desc)
parser.add_argument("--app")
parser.add_argument("--root_dir", default="./")
args = vars(parser.parse_args())

root_dir = args["root_dir"]

os.system(f"python scripts/test_recorder.py --root_dir {root_dir}")

    # os.system(f"python scripts/document_generation.py --app {app} --demo {demo_name} --root_dir {root_dir}")