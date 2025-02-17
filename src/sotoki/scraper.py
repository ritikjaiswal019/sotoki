#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4 nu

import shutil
import pathlib
import datetime
import concurrent.futures as cf

from zimscraperlib.zim.creator import Creator
from zimscraperlib.zim.items import URLItem
from zimscraperlib.inputs import handle_user_provided_file
from zimscraperlib.image.convertion import convert_image
from zimscraperlib.image.transformation import resize_image

from .constants import getLogger, Sotoconf, ROOT_DIR, Global
from .archives import ArchiveManager
from .utils.s3 import setup_s3_and_check_credentials
from .utils.sites import get_site
from .utils.database import get_database
from .utils.imager import Imager
from .utils.html import Rewriter
from .renderer import Renderer
from .users import UserGenerator
from .posts import PostGenerator, PostFirstPasser
from .tags import TagGenerator, TagFinder, TagExcerptRecorder, TagDescriptionRecorder

logger = getLogger()


class StackExchangeToZim:
    def __init__(self, **kwargs):

        Global.conf = Sotoconf(**kwargs)
        for option in Global.conf.required:
            if getattr(Global.conf, option) is None:
                raise ValueError(f"Missing parameter `{option}`")

    @property
    def conf(self):
        return Global.conf

    @property
    def domain(self):
        return self.conf.domain

    @property
    def build_dir(self):
        return self.conf.build_dir

    def cleanup(self):
        """Remove temp files and release resources before exiting"""
        if not self.conf.keep_build_dir:
            logger.debug(f"Removing {self.build_dir}")
            shutil.rmtree(self.build_dir, ignore_errors=True)

    def sanitize_inputs(self):
        """input & metadata sanitation"""

        if self.conf.censor_words_list:
            words_list_fpath = self.build_dir.joinpath("words.list")
            handle_user_provided_file(
                source=self.conf.censor_words_list, dest=words_list_fpath
            )

        period = datetime.datetime.now().strftime("%Y-%m")
        if self.conf.fname:
            # make sure we were given a filename and not a path
            self.conf.fname = pathlib.Path(self.conf.fname.format(period=period))
            if pathlib.Path(self.conf.fname.name) != self.conf.fname:
                raise ValueError(f"filename is not a filename: {self.conf.fname}")
        else:
            self.conf.fname = f"{self.conf.name}_{period}.zim"

        if not self.conf.title:
            self.conf.title = Global.site["LongName"]
        self.conf.title = self.conf.title.strip()

        if not self.conf.description:
            self.conf.description = Global.site["Tagline"]
        self.conf.description = self.conf.description.strip()

        if not self.conf.author:
            self.conf.author = "Stack Exchange"
        self.conf.author = self.conf.author.strip()

        if not self.conf.publisher:
            self.conf.publisher = "Openzim"
        self.conf.publisher = self.conf.publisher.strip()

        self.conf.tags = list(
            set(self.conf.tag + ["_category:stack_exchange", "stack_exchange"])
        )

    def add_illustrations(self):
        src_illus_fpath = self.build_dir / "illustration"

        # if user provided a custom favicon, retrieve that
        if not self.conf.favicon:
            self.conf.favicon = Global.site["BadgeIconUrl"]
        handle_user_provided_file(source=self.conf.favicon, dest=src_illus_fpath)

        # convert to PNG (might already be PNG but it's OK)
        illus_fpath = src_illus_fpath.with_suffix(".png")
        convert_image(src_illus_fpath, illus_fpath)

        # resize to appropriate size (ZIM uses 48x48 so we double for retina)
        resize_image(illus_fpath, width=96, height=96, method="thumbnail")

        Global.creator.add_item_for("illustration", fpath=illus_fpath)

        # download and add actual favicon (ICO file)
        favicon_fpath = self.build_dir / "favicon.ico"
        handle_user_provided_file(source=Global.site["IconUrl"], dest=favicon_fpath)
        Global.creator.add_item_for("favicon.ico", fpath=favicon_fpath)

        # download apple-touch-icon
        Global.creator.add_item(
            URLItem(url=Global.site["BadgeIconUrl"], path="apple-touch-icon.png")
        )

    def add_assets(self):
        assets_root = ROOT_DIR.joinpath("assets")
        with Global.lock:
            for fpath in assets_root.glob("**/*"):
                if not fpath.is_file() or fpath.name == "README":
                    continue
                logger.debug(str(fpath.relative_to(assets_root)))
                Global.creator.add_item_for(
                    path=str(fpath.relative_to(assets_root)), fpath=fpath
                )

        # download primary|secondary.css from target
        assets_base = Global.site["IconUrl"].rsplit("/", 2)[0]
        for css_fname in ("primary.css", "secondary.css", "mobile.css"):
            logger.debug(f"adding {css_fname}")
            Global.creator.add_item(
                URLItem(
                    url=f"{assets_base}/{css_fname}", path=f"static/css/{css_fname}"
                )
            )

    def run(self):
        s3_storage = (
            setup_s3_and_check_credentials(self.conf.s3_url_with_credentials)
            if self.conf.s3_url_with_credentials
            else None
        )

        s3_msg = (
            f"  using cache: {s3_storage.url.netloc} "
            f"with bucket: {s3_storage.bucket_name}"
            if s3_storage
            else ""
        )
        logger.info(
            f"Starting scraper with:\n"
            f"  domain: {self.domain}\n"
            f"  build_dir: {self.build_dir}\n"
            f"  output_dir: {self.conf.output_dir}\n"
            f"{s3_msg}"
        )

        logger.debug("Fetching site details…")
        Global.site = get_site(self.domain)
        if not Global.site:
            logger.critical(
                f"Couldn't fetch detail for {self.domain}. Please check "
                "that it's a supported domain using --list-all."
            )
            return 1

        self.sanitize_inputs()

        logger.info("XML Dumps preparation")
        ark_manager = ArchiveManager()
        ark_manager.check_and_prepare_dumps()
        self.conf.dump_date = ark_manager.get_dump_date()
        del ark_manager

        if self.conf.prepare_only:
            logger.info("Requested preparation only; exiting")
            return

        self.start()

    def start(self):

        try:
            Global.database = get_database()
        except Exception as exc:
            logger.critical("Unable to initialize database. Check --redis-url")
            if Global.debug:
                logger.exception(exc)
            else:
                logger.error(str(exc))
            return 1

        Global.setup(
            imager=Imager(),
            rewriter=Rewriter(),
            # all operations spread accross an nb_threads executor
            executor=cf.ThreadPoolExecutor(max_workers=self.conf.nb_threads),
        )
        # must follow rewriter's assignemnt as t references it
        Global.renderer = Renderer()

        Global.creator = (
            Creator(
                filename=self.conf.output_dir.joinpath(self.conf.fname),
                main_path="questions",
                favicon_path="illustration",
                language="eng",
                title=self.conf.title,
                description=self.conf.description,
                creator=self.conf.author,
                publisher=self.conf.publisher,
                name=self.conf.name,
                tags=";".join(self.conf.tags),
                date=datetime.date.today(),
            )
            .config_nbworkers(self.conf.nb_threads)
            .start()
        )

        try:
            self.add_illustrations()
            self.add_assets()

            # First, walk through Tags and record tags details in DB
            # Then walk through excerpts and record those in DB
            # Then do the same with descriptions
            # Clear the matching that was required for Excerpt/Desc filtering-in
            logger.info("Recording Tag metadata to Database")
            TagFinder().run()
            TagExcerptRecorder().run()
            TagDescriptionRecorder().run()
            Global.database.clear_tags_mapping()
            logger.info(".. done")

            # We walk through all Posts a first time to record question in DB
            # list of users that had interactions
            # list of PostId for all questions
            # list of PostId for all questions of all tags (incr. update)
            # Details for all questions: date, owner, title, excerpt, has_accepted
            logger.info("Recording questions metadata to Database")
            PostFirstPasser().run()
            logger.info(".. done")

            # We walk through all Users and skip all those without interactions
            # Others store basic details in Database
            # Then we create a page in Zim for each user
            # Eventually, we sort our list of users by Reputation
            logger.info("Generating individual Users pages")
            UserGenerator().run()
            Global.database.sort_users()
            logger.info(".. done")

            # We walk again through all Posts, this time to create indiv pages in Zim
            # for each.
            logger.info("Generating Questions pages")
            PostGenerator().run()
            logger.info(".. done")

            # We walk on Tags again, this time creating indiv pages for each Tag.
            # Each tag is actually a number of paginated pages with a list of questions
            logger.info("Generating Tags pages")
            TagGenerator().run()
            logger.info(".. done")

            logger.info("Generating Users page")
            UserGenerator().generate_users_page()
            logger.info(".. done")

            # build home page in ZIM using questions list
            logger.info("Generating Questions page (homepage)")
            PostGenerator().generate_questions_page()
            with Global.lock:
                Global.creator.add_item_for(
                    path="about",
                    title="About",
                    content=Global.renderer.get_about_page(),
                    mimetype="text/html",
                )
                Global.creator.add_redirect(path="", target_path="questions")
            logger.info(".. done")

            Global.database.teardown()
            Global.database.remove()

            Global.executor.shutdown()

        except KeyboardInterrupt:
            Global.creator.can_finish = False
            logger.error("KeyboardInterrupt, exiting.")
        except Exception as exc:
            # request Creator not to create a ZIM file on finish
            Global.creator.can_finish = False
            logger.error(f"Interrupting process due to error: {exc}")
            logger.exception(exc)
        finally:
            logger.info("Finishing ZIM file…")
            # we need to release libzim's resources.
            # currently does nothing but crash if can_finish=False but that's awaiting
            # impl. at libkiwix level
            with Global.lock:
                Global.creator.finish()
            logger.info("Zim finished")
