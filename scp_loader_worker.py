import argparse

from scp_loader import update_archive


def run_worker(download_media=False):
    return update_archive(download_media=download_media)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update the local SCP archive.")
    parser.add_argument(
        "--media",
        action="store_true",
        help="download article images during the update",
    )
    raise SystemExit(run_worker(download_media=parser.parse_args().media))
