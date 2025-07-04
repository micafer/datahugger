import hashlib
import io
import json
import logging
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Union
from urllib.parse import urlparse

import requests
from jsonpath_ng.ext import parse
from scitree import scitree
from tqdm import tqdm

from datahugger.utils import _format_filename
from datahugger.utils import _get_url
from datahugger.utils import _is_url


class DownloadResult:
    """Result class after downloading the dataset."""

    def __init__(self, dataset, output_folder):
        self.dataset = dataset
        self.output_folder = output_folder

    def __str__(self):
        return f"<{self.__class__.__name__} n_files={len(self)} >"

    def __len__(self):
        return len(self.dataset.files)

    def tree(self, **kwargs):
        """Return the folder tree.

        Tree based on scientific sort.
        """

        return scitree(self.output_folder, **kwargs)


class DatasetDownloader:
    """Base class for downloading resources from repositories."""

    API_URL = None

    def __init__(
        self,
        resource,
        max_file_size=None,
        filter_files=None,
        force_download=False,
        progress=True,
        unzip=True,
        checksum=False,
        print_only=False,
        params=None,
    ):
        super().__init__()
        self.resource = resource
        self.max_file_size = max_file_size
        self.filter_files = filter_files
        self.force_download = force_download
        self.progress = progress
        self.unzip = unzip
        self.checksum = checksum
        self.print_only = print_only
        self.params = params

    def _get_attr_attr(self, record, jsonp):
        try:
            jsonpath_expression = parse(jsonp)
            return jsonpath_expression.find(record)[0].value
        except Exception:
            return None

    def _get_attr_link(self, record, **kwargs):
        # get the link to the folder
        if self._get_attr_kind(record) == "folder":
            if not hasattr(self, "ATTR_FOLDER_LINK_JSONPATH"):
                return None

            return self._get_attr_attr(record, self.ATTR_FOLDER_LINK_JSONPATH)

        # get the link to the file
        else:
            if not hasattr(self, "ATTR_FILE_LINK_JSONPATH"):
                return None

            return self._get_attr_attr(record, self.ATTR_FILE_LINK_JSONPATH)

    def _get_attr_name(self, record):
        if not hasattr(self, "ATTR_NAME_JSONPATH"):
            return None

        return self._get_attr_attr(record, self.ATTR_NAME_JSONPATH)

    def _get_attr_size(self, record):
        if not hasattr(self, "ATTR_SIZE_JSONPATH"):
            return None

        return self._get_attr_attr(record, self.ATTR_SIZE_JSONPATH)

    def _get_attr_hash(self, record):
        if not hasattr(self, "ATTR_HASH_JSONPATH"):
            return None

        return self._get_attr_attr(record, self.ATTR_HASH_JSONPATH)

    def _get_attr_hash_type(self, record):
        if hasattr(self, "ATTR_HASH_TYPE_VALUE"):
            return self.ATTR_HASH_TYPE_VALUE

        if not hasattr(self, "ATTR_HASH_TYPE_JSONPATH"):
            return None

        return self._get_attr_attr(record, self.ATTR_HASH_TYPE_JSONPATH)

    def _get_attr_kind(self, record):
        if not hasattr(self, "ATTR_KIND_JSONPATH"):
            return "file"

        return self._get_attr_attr(record, self.ATTR_KIND_JSONPATH)

    def download_file(
        self,
        file_link,
        output_folder,
        file_name,
        file_size=None,
        file_hash=None,
        file_hash_type=None,
    ):
        """Download a single file.

        Arguments
        ---------
        file_link: str
            Path to the file to download.
        output_folder: str
            The folder to store the downloaded file.
        file_name: str
            The filename of the downloaded file.
        file_size: int
            The size of the file in bytes.
        file_hash: str
            The MD5 hash of the file.

        """
        if (
            file_size is not None
            and self.max_file_size is not None
            and file_size >= self.max_file_size
        ):
            logging.info(f"Skipping large file {file_link}")
            if self.progress:
                print(f"{_format_filename(file_name)}: SKIPPED")
            return

        if self.filter_files and not re.match(self.filter_files, file_name):
            logging.info(f"Skipping file by filter {file_link}")
            if self.progress:
                print(f"{_format_filename(file_name)}: SKIPPED")
            return

        if not self.print_only:
            logging.info(f"Downloading file {file_link}")
            res = requests.get(file_link, stream=True)
            res.raise_for_status()

            output_fp = Path(output_folder, file_name)
            Path(output_fp).parent.mkdir(parents=True, exist_ok=True)

            if not self.force_download and output_fp.exists():
                print("File already exists:", file_name)
                return

            if self.progress:
                with tqdm.wrapattr(
                    open(output_fp, "wb"),
                    "write",
                    miniters=1,
                    desc=_format_filename(file_name),
                    total=int(res.headers.get("content-length", 0)),
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}",
                ) as fout:
                    for chunk in res.iter_content(chunk_size=4096):
                        fout.write(chunk)
            else:
                with open(output_fp, "wb") as f:
                    f.write(res.content)
        else:
            print(f"{_format_filename(file_name)}: COMPLETE")

    def _parse_url(self, url):
        if not isinstance(url, str) or not _is_url(url):
            raise ValueError("Not a valid URL.")

        try:
            match = re.search(self.REGEXP_ID, url)
            return match.groupdict()
        except Exception as err:
            raise ValueError(f"Failed to parse URL '{url}'") from err

    def _unpack_single_folder(self, zip_url, output_folder):
        r = requests.get(zip_url)
        r.raise_for_status()

        z = zipfile.ZipFile(io.BytesIO(r.content))

        for zip_info in z.infolist():
            if zip_info.filename[-1] == "/":
                continue
            zip_info.filename = os.path.basename(zip_info.filename)
            z.extract(zip_info, output_folder)

    def _check_checksums(self, output_folder, files_info):
        """Will compare the checksum values in the files_info with the checksums
        of the downloaded files and will create a file in a new 'generated'
        folder with the results.

        Args:
            output_folder (str): output_folder to push the data to
            files_info (list): information on all the files

        Example file output:
            file name: generated/checksums.json
            file content:
                {"BTCBRL_final.csv": true}
        """
        try:
            checksums = {}

            # loop through the downloaded files in the output_folder
            for subdir, dirs, files in os.walk(output_folder):
                logging.info(f"Not using the dirs: {dirs}")
                for file in files:
                    filepath = os.path.join(subdir, file)

                    file_comp = list(filter(lambda x: x["name"] == file, files_info))

                    try:
                        hash = file_comp[0]["hash"]
                        hash_type = file_comp[0]["hash_type"]
                    except IndexError:
                        logging.info("Setting hash and hash_type to None")
                        hash = None
                        hash_type = None

                    newhash = None
                    with open(filepath, "rb") as f:
                        if hash_type == "md5":
                            newhash = hashlib.md5(f.read()).hexdigest()
                        if hash_type == "sha1":
                            newhash = hashlib.sha1(f.read()).hexdigest()
                        if hash_type == "sha224":
                            newhash = hashlib.sha224(f.read()).hexdigest()
                        if hash_type == "sha256":
                            newhash = hashlib.sha256(f.read()).hexdigest()
                        if hash_type == "sha384":
                            newhash = hashlib.sha384(f.read()).hexdigest()
                        if hash_type == "sha512":
                            newhash = hashlib.sha512(f.read()).hexdigest()
                    hash_match = hash == newhash

                    if hash is not None and hash_type is not None:
                        print(f"Checksum match: {hash_match} - {file}")
                        logging.info(f"Checksum match: {hash_match} - {file}")
                        checksums[file] = hash_match

            try:
                timestamp = str(time.time()).split(".")[0]
            except Exception as e:
                logging.info(f"Setting timestamp to empty string: {e}")
                timestamp = ""
            generated_path = f"{output_folder}/generated"
            if not os.path.isdir(generated_path):
                os.mkdir(generated_path)
            with open(f"{generated_path}/checksums{timestamp}.json", "w") as f:
                json.dump(checksums, f)
        except Exception as e:
            logging.error(f"Failed at checksum: {e}")

    def _pre_files(self):
        pass

    def _get_files_recursive(self, url, folder_name=None, base_url=None):
        if not isinstance(url, str):
            ValueError(f"Expected url to be string type, got {type(url)}")

        result = []

        # get the data from URL
        res = requests.get(url)
        res.raise_for_status()
        response = res.json()

        # find path to raw files
        if hasattr(self, "META_FILES_JSONPATH"):
            if isinstance(self.META_FILES_JSONPATH, str):
                jsonpath_expression = parse(self.META_FILES_JSONPATH)
            else:
                jsonpath_expression = self.META_FILES_JSONPATH

            files_raw = [x.value for x in jsonpath_expression.find(response)]
        else:
            files_raw = response

        for f in files_raw:
            # create the file or folder path
            if folder_name is None:
                f_path = self._get_attr_name(f)
            else:
                f_path = str(Path(folder_name, self._get_attr_name(f)))

            if self._get_attr_kind(f) == "folder":
                result.extend(
                    self._get_files_recursive(
                        self._get_attr_link(f, base_url=base_url), folder_name=f_path
                    )
                )
            else:
                result.append(
                    {
                        "link": self._get_attr_link(f, base_url=base_url),
                        "name": f_path,
                        "size": self._get_attr_size(f),
                        "hash": self._get_attr_hash(f),
                        "hash_type": self._get_attr_hash_type(f),
                    }
                )

        if hasattr(self, "PAGINATION_JSONPATH"):
            jsonpath_expression = parse(self.PAGINATION_JSONPATH)
            next_url = jsonpath_expression.find(response)[0].value

            if next_url:
                result.extend(
                    self._get_files_recursive(next_url, folder_name=folder_name)
                )

        return result

    @property
    def _params(self):
        """Params including url params."""
        if hasattr(self, "__params"):
            return self.__params

        url = _get_url(self.resource)
        url_params = self._parse_url(url)
        if self.params:
            new_params = self.params.copy()
            new_params.update(url_params)
            self.__params = new_params
        else:
            self.__params = url_params

        return self.__params

    @property
    def files(self):
        if hasattr(self, "_files"):
            return self._files

        self._pre_files()

        url = _get_url(self.resource)
        uri = urlparse(url)
        base_url = uri.scheme + "://" + uri.netloc

        self._files = self._get_files_recursive(
            self.API_URL_META.format(
                api_url=self.API_URL, base_url=base_url, **self._params
            ),
            base_url=base_url,
        )

        return self._files

    def _get(
        self,
        output_folder: Union[Path, str],
    ):
        if (
            len(self.files) == 1
            and (
                self.files[0]["link"].endswith(".zip")
                or self.files[0]["name"].endswith(".zip")
            )
            and self.unzip
        ):
            self._unpack_single_folder(self.files[0]["link"], output_folder)
            return

        files_info = []
        for f in self.files:
            files_info.append(f)
            self.download_file(
                f["link"],
                output_folder,
                file_name=f["name"],
                file_size=f["size"],
                file_hash=f["hash"],
                file_hash_type=f["hash_type"],
            )

        if self.checksum:
            self._check_checksums(output_folder=output_folder, files_info=files_info)

    def download(
        self,
        output_folder: Union[Path, str],
    ):
        """Download files.

        Arguments
        ---------
        output_folder: str
            The folder to store the downloaded results.

        """

        self._get(output_folder=output_folder)

        return DownloadResult(self, output_folder)
