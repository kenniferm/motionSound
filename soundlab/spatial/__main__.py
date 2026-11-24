"""Install the measured FABIAN dataset."""
import argparse
import json
import os
from pathlib import Path

from .dataset import import_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', default=os.getenv('SOUNDLAB_DATA_DIR', 'data'), type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('import-dataset').add_argument('path', type=Path)
    args = parser.parse_args()
    print(json.dumps(import_dataset(args.path, args.data), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
