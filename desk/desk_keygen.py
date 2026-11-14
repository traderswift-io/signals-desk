import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from nacl.signing import SigningKey

from .desk_record import sync_file


def generate_key(config):
    path = Path(config['delegate_key_file'])
    key = SigningKey.generate()
    with TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / 'seed'
        with temporary.open('x') as stream:
            temporary.chmod(0o600)
            stream.write(key.encode().hex() + '\n')
            sync_file(stream)
        path.hardlink_to(temporary)
    return key.verify_key.encode().hex()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    arguments = parser.parse_args()
    print(generate_key(json.loads(Path(arguments.config).read_text())))
