#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4 nu

import datetime
import concurrent.futures as cf

import requests
import dateparser
from zimscraperlib.download import stream_file, save_large_file

from .constants import getLogger, Global
from .utils.misc import has_binary
from .utils.sevenzip import extract_7z
from .utils.preparation import (
    merge_users_with_badges,
    merge_posts_with_answers_comments,
)

logger = getLogger()


class ArchiveManager:
    """Handle retrieval and processing of StackExchange dump files

    Each website is available as a single 7z archive
    except stackoverflow which is split in multiple ones

    7z files extracts to a number of XML files. We are interested in a few
    that we need to read and combine (and thus sort).

    Manipulations of the XML files is done in preparation module.

    As this is a lenghty process (several hours for SO) and the output doesn't
    change until next dump (twice a year), this handles reusing existing files"""

    @property
    def build_dir(self):
        return Global.conf.build_dir

    @property
    def domain(self):
        return Global.conf.domain

    @property
    def mirror(self):
        return Global.conf.mirror

    @property
    def delete_src(self):
        return not Global.conf.keep_intermediate_files

    @property
    def dump_parts(self):
        """XML Dump files we're interested in"""
        return ("Badges", "Comments", "PostLinks", "Posts", "Tags", "Users")

    @property
    def archives(self):
        """list of 7z archive files"""
        if self.domain != "stackoverflow.com":
            return [self.build_dir / f"{self.domain}.7z"]
        return [self.build_dir / f"{self.domain}-{part}.7z" for part in self.dump_parts]

    def get_dump_date(self):
        """date indicating the month and year the dump ark was produced"""
        resp = requests.head(url=f"{self.mirror}/{self.archives[0].name}")
        header = resp.headers.get("Last-Modified")
        if header:
            try:
                return dateparser.parse(header)
            except ValueError:
                pass
        return datetime.date.now()  # default to today

    def download_and_extract_archives(self):
        logger.info("Downloading archive(s)…")

        # use wget for downloading 7z files if available
        download = save_large_file if has_binary("wget") else stream_file

        def _run(url, fpath):
            if not fpath.exists():
                download(url, fpath)
            extract_7z(fpath, self.build_dir, delete_src=self.delete_src)
            # remove other files from ark that we won't need
            for fpath in self.build_dir.iterdir():
                if fpath.suffix == ".xml" and fpath.stem not in self.dump_parts:
                    fpath.unlink()

        futures = {}
        executor = cf.ThreadPoolExecutor(max_workers=len(self.archives))

        for ark in self.archives:
            url = f"{self.mirror}/{ark.name}"
            kwargs = {"url": url, "fpath": ark}
            future = executor.submit(_run, **kwargs)
            futures.update({future: kwargs})

        result = cf.wait(futures.keys(), return_when=cf.FIRST_EXCEPTION)
        executor.shutdown()

        failed = False
        for future in result.done:
            exc = future.exception()
            if exc:
                item = futures.get(future)
                logger.error(f"Error processing {item['fpath'].name}: {exc}")
                logger.exception(exc)
                failed = True

        if not failed and result.not_done:
            logger.error(
                "Some not_done futrues: \n - "
                + "\n - ".join([futures.get(future) for future in result.not_done])
            )
            failed = True

        if failed:
            raise Exception("Unable to complete download and extraction")

    def check_and_prepare_dumps(self):
        tags = self.build_dir / "Tags.xml"
        users = self.build_dir / "users_with_badges.xml"
        posts = self.build_dir / "posts_complete.xml"

        # check what needs to be done for each substep in order to reuse existing files
        if not tags.exists() or not users.exists() or not posts.exists():
            if not all(
                [
                    self.build_dir.joinpath(f"{part}.xml").exists()
                    for part in self.dump_parts
                ]
            ):
                self.download_and_extract_archives()
            else:
                logger.info("Extracted parts present; reusing")
        else:
            logger.info("Prepared dumps already present; reusing.")
            return

        if not tags.exists():
            raise IOError(f"Missing {tags.name} while we should not.")

        merge_users_with_badges(workdir=self.build_dir, delete_src=self.delete_src)
        if not users.exists():
            raise IOError(f"Missing {users.name} while we should not.")

        merge_posts_with_answers_comments(
            workdir=self.build_dir, delete_src=self.delete_src
        )
        if not posts.exists():
            raise IOError(f"Missing {posts.name} while we should not.")

        logger.info("Prepared dumps completed.")
