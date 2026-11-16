import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from nacl.signing import SigningKey

from .desk_private_storage import prepare_private_directories, private_open
from .desk_record import sync_file


def generate_key(config):
    path = Path(config['delegate_key_file'])
    base = Path(config.get('_config_directory', path.absolute().parent))
    prepare_private_directories([path.parent], base)
    key = SigningKey.generate()
    with TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / 'seed'
        with private_open(temporary, 'x') as stream:
            stream.write(key.encode().hex() + '\n')
            sync_file(stream)
        path.hardlink_to(temporary)
    return key.verify_key.encode().hex()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    arguments = parser.parse_args()
    configuration = json.loads(Path(arguments.config).read_text())
    configuration['_config_directory'] = str(Path(arguments.config).absolute().parent)
    print(generate_key(configuration))
