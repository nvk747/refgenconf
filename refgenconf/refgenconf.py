#!/usr/bin/env python

from collections import Iterable, Mapping, OrderedDict
from functools import partial

# Some short-term hacks to get at least 1 working version on python 2.7
import sys
if sys.version_info >= (3, ):
    from inspect import getfullargspec as finspect
    from urllib.error import HTTPError, ContentTooShortError
else:
    from future.standard_library import install_aliases
    install_aliases()
    from inspect import getargspec as finspect
    from urllib2 import HTTPError
    from urllib.error import ContentTooShortError
    ConnectionRefusedError = Exception

import urllib.request
import itertools
import logging
import os
import signal
import warnings
import shutil

from attmap import PathExAttMap as PXAM
from ubiquerg import checksum, is_url, query_yes_no, parse_registry_path as prp, untar
from tqdm import tqdm

import yacman

from .const import *
from .helpers import unbound_env_vars, asciify_dict
from .exceptions import *


_LOGGER = logging.getLogger(__name__)


__all__ = ["RefGenConf"]


def _handle_sigint(filepath, rgc):
    def handle(sig, frame):
        _LOGGER.warning("\nThe download was interrupted: {}".format(filepath))
        try:
            os.remove(filepath)
        except OSError:
            _LOGGER.debug("'{}' not found, can't remove".format(filepath))
        else:
            _LOGGER.info("Incomplete file '{}' was removed".format(filepath))
        sys.exit(0)
    return handle


class RefGenConf(yacman.YacAttMap):
    """ A sort of oracle of available reference genome assembly assets """

    def __init__(self, filepath=None, entries=None, writable=False, wait_max=10):
        """
        Create the config instance by with a filepath or key-value pairs.

        :param str filepath: a path to the YAML file to read
        :param Iterable[(str, object)] | Mapping[str, object] entries:
            config filepath or collection of key-value pairs
        :param bool writable: whether to create the object with write capabilities
        :param int wait_max: how long to wait for creating an object when the file that data will be read from is locked
        :raise refgenconf.MissingConfigDataError: if a required configuration
            item is missing
        :raise ValueError: if entries is given as a string and is not a file
        """
        super(RefGenConf, self).__init__(filepath=filepath, entries=entries, writable=writable, wait_max=wait_max)
        genomes = self.setdefault(CFG_GENOMES_KEY, PXAM())
        if not isinstance(genomes, PXAM):
            if genomes:
                _LOGGER.warning("'{k}' value is a {t_old}, not a {t_new}; setting to empty {t_new}".
                                format(k=CFG_GENOMES_KEY, t_old=type(genomes).__name__, t_new=PXAM.__name__))
            self[CFG_GENOMES_KEY] = PXAM()
        if CFG_FOLDER_KEY not in self:
            self[CFG_FOLDER_KEY] = os.path.dirname(entries) if isinstance(entries, str) else os.getcwd()
        try:
            version = self[CFG_VERSION_KEY]
        except KeyError:
            _LOGGER.warning("Config lacks version key: {}".format(CFG_VERSION_KEY))
        else:
            try:
                version = float(version)
            except ValueError:
                _LOGGER.warning("Cannot parse config version as numeric: {}".format(version))
            else:
                if version < REQ_CFG_VERSION:
                    msg = "This genome config (v{}) is not compliant with v{} standards. To use it, please downgrade " \
                          "refgenie: 'pip install refgenie=={}'.".format(self[CFG_VERSION_KEY], str(REQ_CFG_VERSION),
                                                                         REFGENIE_BY_CFG[str(version)])
                    raise ConfigNotCompliantError(msg)
                else:
                    _LOGGER.debug("Config version is compliant: {}".format(version))
        try:
            self[CFG_SERVER_KEY] = self[CFG_SERVER_KEY].rstrip("/")
        except KeyError:
            raise MissingConfigDataError(CFG_SERVER_KEY)

    def __bool__(self):
        minkeys = set(self.keys()) == {CFG_SERVER_KEY, CFG_FOLDER_KEY, CFG_GENOMES_KEY}
        return not minkeys or bool(self[CFG_GENOMES_KEY])

    __nonzero__ = __bool__

    def assets_dict(self, genome=None, order=None, include_tags=False):
        """
        Map each assembly name to a list of available asset names.

        :param function(str) -> object order: how to key genome IDs for sort
        :param list[str] | str genome: genomes that the assets should be found for
        :param bool include_tags: whether asset tags should be included in the returned dict
        :return Mapping[str, Iterable[str]]: mapping from assembly name to
            collection of available asset names.
        """
        refgens = _select_genomes(sorted(self[CFG_GENOMES_KEY].keys(), key=order), genome)
        if include_tags:
            return OrderedDict(
                [(g, sorted(_make_asset_tags_product(self[CFG_GENOMES_KEY][g][CFG_ASSETS_KEY], ":"), key=order))
                 for g in refgens])
        return OrderedDict([(g, sorted(list(self[CFG_GENOMES_KEY][g][CFG_ASSETS_KEY].keys()), key=order))
                            for g in refgens])

    def assets_str(self, offset_text="  ", asset_sep=", ", genome_assets_delim="/ ", genome=None, order=None):
        """
        Create a block of text representing genome-to-asset mapping.

        :param str offset_text: text that begins each line of the text
            representation that's produced
        :param str asset_sep: the delimiter between names of types of assets,
            within each genome line
        :param str genome_assets_delim: the delimiter to place between
            reference genome assembly name and its list of asset names
        :param list[str] | str genome: genomes that the assets should be found for
        :param order: function(str) -> object how to key genome IDs and asset
            names for sort
        :return str: text representing genome-to-asset mapping
        """
        refgens = _select_genomes(sorted(self[CFG_GENOMES_KEY].keys(), key=order), genome)
        make_line = partial(_make_genome_assets_line, offset_text=offset_text, genome_assets_delim=genome_assets_delim,
                            asset_sep=asset_sep, order=order)
        return "\n".join([make_line(g, self[CFG_GENOMES_KEY][g][CFG_ASSETS_KEY]) for g in refgens])

    def filepath(self, genome, asset, tag, ext=".tgz"):
        """
        Determine path to a particular asset for a particular genome.

        :param str genome: reference genome ID
        :param str asset: asset name
        :param str tag: tag name
        :param str ext: file extension
        :return str: path to asset for given genome and asset kind/name
        """
        return os.path.join(self[CFG_FOLDER_KEY], genome, asset, tag, asset + "__" + tag + ext)

    def genomes_list(self, order=None):
        """
        Get a list of this configuration's reference genome assembly IDs.

        :return Iterable[str]: list of this configuration's reference genome
            assembly IDs
        """
        return sorted(list(self[CFG_GENOMES_KEY].keys()), key=order)

    def genomes_str(self, order=None):
        """
        Get as single string this configuration's reference genome assembly IDs.

        :param order: function(str) -> object how to key genome IDs for sort
        :return str: single string that lists this configuration's known
            reference genome assembly IDs
        """
        return ", ".join(self.genomes_list(order))

    def get_asset(self, genome_name, asset_name, tag_name=None, seek_key=None, strict_exists=True,
                  check_exist=lambda p: os.path.exists(p) or is_url(p), enclosing_dir=False):
        """
        Get an asset for a particular assembly.

        :param str genome_name: name of a reference genome assembly of interest
        :param str asset_name: name of the particular asset to fetch
        :param str tag_name: name of the particular asset tag to fetch
        :param str seek_key: name of the particular subasset to fetch
        :param bool | NoneType strict_exists: how to handle case in which
            path doesn't exist; True to raise IOError, False to raise
            RuntimeWarning, and None to do nothing at all
        :param function(callable) -> bool check_exist: how to check for
            asset/path existence
        :param bool enclosing_dir: whether a path to the entire enclosing directory should be returned, e.g.
            for a fasta asset that has 3 seek_keys pointing to 3 files in an asset dir, that asset dir is returned
        :return str: path to the asset
        :raise TypeError: if the existence check is not a one-arg function
        :raise refgenconf.MissingGenomeError: if the named assembly isn't known
            to this configuration instance
        :raise refgenconf.MissingAssetError: if the names assembly is known to
            this configuration instance, but the requested asset is unknown
        """
        tag_name = tag_name or self.get_default_tag(genome_name, asset_name)
        _LOGGER.debug("getting asset: '{}/{}.{}:{}'".format(genome_name, asset_name, seek_key, tag_name))
        if not callable(check_exist) or len(finspect(check_exist).args) != 1:
            raise TypeError("Asset existence check must be a one-arg function.")
        path = _genome_asset_path(self[CFG_GENOMES_KEY], genome_name, asset_name, tag_name, seek_key, enclosing_dir)
        if os.path.isabs(path) and check_exist(path):
            return path
        _LOGGER.debug("Relative or nonexistent path: {}".format(path))
        fullpath = os.path.join(self[CFG_FOLDER_KEY], genome_name, path)
        _LOGGER.debug("Trying path relative to genome folder: {}".format(fullpath))
        if check_exist(fullpath):
            return fullpath
        elif strict_exists is None:
            return path
        msg = "For genome '{}' the asset '{}.{}:{}' doesn't exist; tried {} and {}".\
            format(genome_name, asset_name, seek_key, tag_name, path, fullpath)
        extant = []
        for base, ext in itertools.product([path, fullpath], [".tar.gz", ".tar"]):
            # Attempt to enrich message with extra guidance.
            p_prime = base + ext
            if check_exist(p_prime):
                extant.append(p_prime)
        if extant:
            msg += ". These paths exist: {}".format(extant)
        if strict_exists is True:
            raise IOError(msg)
        else:
            warnings.warn(msg, RuntimeWarning)
        return path

    def get_default_tag(self, genome, asset, use_existing=True):
        """
        Determine the asset tag to use as default. The one indicated by the 'default_tag' key in the asset
        section is returned.
        If no 'default_tag' key is found, by default the first listed tag is returned with a RuntimeWarning.
        This behavior can be turned off with use_existing=False

        :param str genome: name of a reference genome assembly of interest
        :param str asset: name of the particular asset of interest
        :param bool use_existing: whether the first tag in the config should be returned in case there is no default
        tag defined for an asset
        :return str: name of the tag to use as the default one
        """
        try:
            _assert_gat_exists(self[CFG_GENOMES_KEY], genome, asset)
        except Exception as e:
            _LOGGER.info("{}: using '{}' as the default tag".format(e.__class__.__name__, DEFAULT_TAG))
            return DEFAULT_TAG
        try:
            return self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_DEFAULT_TAG_KEY]
        except KeyError:
            alt = self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY].keys()[0] if use_existing\
                else DEFAULT_TAG
            if isinstance(alt, str):
                if alt != DEFAULT_TAG:
                    warnings.warn("Could not find the '{}' key for asset '{}/{}'. "
                                  "Used the first one in the config instead: '{}'. "
                                  "Make sure it does not corrupt your workflow."
                                  .format(CFG_ASSET_DEFAULT_TAG_KEY, genome, asset, alt), RuntimeWarning)
                else:
                    warnings.warn("Could not find the '{}' key for asset '{}/{}'. "
                                  "Returning '{}' instead. Make sure it does not corrupt your workflow."
                                  .format(CFG_ASSET_DEFAULT_TAG_KEY, genome, asset, alt), RuntimeWarning)
                return alt

    def set_default_pointer(self, genome, asset, tag, force=False):
        """
        Point to the selected tag by default

        :param str genome: name of a reference genome assembly of interest
        :param str asset: name of the particular asset of interest
        :param str tag: name of the particular asset tag to point to by default
        :param bool force: whether the default tag change should be forced (even if it exists)
        """
        _assert_gat_exists(self[CFG_GENOMES_KEY], genome, asset)
        if CFG_ASSET_DEFAULT_TAG_KEY not in self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset] or \
                len(self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_DEFAULT_TAG_KEY]) == 0 or force:
            self.update_assets(genome, asset, {CFG_ASSET_DEFAULT_TAG_KEY: tag})
            _LOGGER.info("Default tag for '{}/{}' set to: {}".format(genome, asset, tag))

    def list_assets_by_genome(self, genome=None, order=None, include_tags=False):
        """
        List types/names of assets that are available for one--or all--genomes.

        :param str | NoneType genome: reference genome assembly ID, optional;
            if omitted, the full mapping from genome to asset names
        :param order: function(str) -> object how to key genome IDs and asset
            names for sort
        :param bool include_tags: whether asset tags should be included in the returned dict
        :return Iterable[str] | Mapping[str, Iterable[str]]: collection of
            asset type names available for particular reference assembly if
            one is provided, else the full mapping between assembly ID and
            collection available asset type names
        """
        return self.assets_dict(genome, order, include_tags=include_tags)[genome] if genome is not None \
            else self.assets_dict(order, include_tags=include_tags)

    def list_genomes_by_asset(self, asset=None, order=None):
        """
        List assemblies for which a particular asset is available.

        :param str | NoneType asset: name of type of asset of interest, optional
        :param order: function(str) -> object how to key genome IDs and asset
            names for sort
        :return Iterable[str] | Mapping[str, Iterable[str]]: collection of
            assemblies for which the given asset is available; if asset
            argument is omitted, the full mapping from name of asset type to
            collection of assembly names for which the asset key is available
            will be returned.
        """
        return self._invert_genomes(order) if not asset else \
            sorted([g for g, data in self[CFG_GENOMES_KEY].items()
                    if asset in data.get(CFG_ASSETS_KEY)], key=order)

    def list_local(self, genome=None, order=None):
        """
        List locally available reference genome IDs and assets by ID.

        :param list[str] | str genome: genomes that the assets should be found for
        :param order: function(str) -> object how to key genome IDs and asset
            names for sort
        :return str, str: text reps of locally available genomes and assets
        """
        if genome is not None:
            _assert_gat_exists(self[CFG_GENOMES_KEY], gname=genome)
        genomes_str = self.genomes_str(order=order) if genome is None \
            else ", ".join(_select_genomes(sorted(self[CFG_GENOMES_KEY].keys(), key=order), genome))
        return genomes_str, self.assets_str(genome=genome, order=order)

    def list_remote(self, get_url=lambda rgc, v: "{}/{}/assets".format(rgc.genome_server, v), genome=None, order=None):
        """
        List genomes and assets available remotely.

        :param function(refgenconf.RefGenConf) -> str get_url: how to determine
            URL request, given RefGenConf instance
        :param list[str] | str genome: genomes that the assets should be found for
        :param order: function(str) -> object how to key genome IDs and asset
            names for sort
        :return str, str: text reps of remotely available genomes and assets
        """
        url = get_url(self, API_VERSION)
        _LOGGER.info("Querying available assets from server: {}".format(url))
        genomes, assets = _list_remote(url, genome, order)
        return genomes, assets

    def tag_asset(self, genome, asset, tag, new_tag):
        """
        Retags the asset selected by the tag with the new_tag.
        Prompts if default already exists and overrides upon confirmation.

        This method does not override the original asset entry in the RefGenConf object. It creates its copy and tags
        it with the new_tag.
        Additionally, if the retagged asset has any children their parent will be retagged as new_tag that was
        introduced upon this method execution.

        :param str genome: name of a reference genome assembly of interest
        :param str asset: name of particular asset of interest
        :param str tag: name of the tag that identifies the asset of interest
        :param str new_tag: name of particular the new tag
        :raise ValueError: when the original tag is not specified
        :return bool: a logical indicating whether the tagging was successful
        """
        _assert_gat_exists(self[CFG_GENOMES_KEY], genome, asset, tag)
        asset_mapping = self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset]
        if tag is None:
            raise ValueError("You must explicitly specify the tag of the asset "
                             "you want to reassign. \nCurrently defined "
                             "tags for '{}/{}' are: {}".format(genome, asset,", ".join(get_asset_tags(asset_mapping))))
        if new_tag in asset_mapping[CFG_ASSET_TAGS_KEY]:
            if not query_yes_no("You already have a '{}' asset tagged as '{}', do you wish to override?".
                                        format(asset, new_tag)):
                _LOGGER.info("Tag action aborted by the user")
                return
        children = []
        parents = []
        if CFG_ASSET_CHILDREN_KEY in asset_mapping[CFG_ASSET_TAGS_KEY][tag]:
            children = asset_mapping[CFG_ASSET_TAGS_KEY][tag][CFG_ASSET_CHILDREN_KEY]
        if CFG_ASSET_PARENTS_KEY in asset_mapping[CFG_ASSET_TAGS_KEY][tag]:
            parents = asset_mapping[CFG_ASSET_TAGS_KEY][tag][CFG_ASSET_PARENTS_KEY]
        if len(children) > 0 or len(parents) > 0:
            if not query_yes_no("The asset '{}/{}:{}' has {} children and {} parents. Refgenie will update the "
                                "relationship data. Do you want to proceed?".format(genome, asset, tag, len(children),
                                                                                    len(parents))):
                _LOGGER.info("Tag action aborted by the user")
                return False
            # updates children's parents
            self._update_relatives_tags(genome, asset, tag, new_tag, children, update_children=False)
            # updates parents' children
            self._update_relatives_tags(genome, asset, tag, new_tag, parents, update_children=True)
        self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY][new_tag] = \
            asset_mapping[CFG_ASSET_TAGS_KEY][tag]
        if CFG_ASSET_DEFAULT_TAG_KEY in asset_mapping and asset_mapping[CFG_ASSET_DEFAULT_TAG_KEY] == tag:
            self.set_default_pointer(genome, asset, new_tag, force=True)
        self.remove_assets(genome, asset, tag)
        return True

    def _update_relatives_tags(self, genome, asset, tag, new_tag, relatives, update_children):
        """
        Internal method used for tags updating in the 'asset_parents' section in the list of children.

        :param str genome: name of a reference genome assembly of interest
        :param str asset: name of particular asset of interest
        :param str tag: name of the tag that identifies the asset of interest
        :param str new_tag: name of particular the new tag
        :param list[str] relatives: relatives to be updated. Format: ["asset_name:tag", "asset_name1:tag1"]
        :param bool update_children: whether the children of the selected relatives should be updated.
        """
        relative_key = CFG_ASSET_CHILDREN_KEY if update_children else CFG_ASSET_PARENTS_KEY
        for r in relatives:
            _LOGGER.debug("updating {} in '{}'".format("children" if update_children else "parents", r))
            r_data = prp(r)
            try:
                self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][r_data["item"]][CFG_ASSET_TAGS_KEY][r_data["tag"]]
            except KeyError:
                _LOGGER.warning("The {} asset of '{}/{}' does not exist: {}".
                                format("parent" if update_children else "child", genome, asset, r))
                continue
            updated_relatives = []
            if relative_key in \
                    self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][r_data["item"]][CFG_ASSET_TAGS_KEY][r_data["tag"]]:
                relatives = \
                    self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][r_data["item"]][CFG_ASSET_TAGS_KEY][r_data["tag"]]\
                        [relative_key]
                for relative in relatives:
                    ori_relative_data = prp(relative)
                    if ori_relative_data["item"] == asset and ori_relative_data["tag"] == tag:
                        ori_relative_data["tag"] = new_tag
                        updated_relatives.append("{}:{}".format(asset, new_tag))
                    else:
                        updated_relatives.append("{}:{}".format(ori_relative_data["item"], ori_relative_data["tag"]))
            self.update_relatives_assets(genome, r_data["item"], r_data["tag"], updated_relatives, update_children)
            self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][r_data["item"]][CFG_ASSET_TAGS_KEY][r_data["tag"]]\
                [relative_key] = updated_relatives

    def pull_asset(self, genome, asset, tag, unpack=True, force=None,
                   get_json_url=lambda base, v, g, a: "{}/{}/asset/{}/{}".format(base, v, g, a),
                   build_signal_handler=_handle_sigint):
        """
        Download and possibly unpack one or more assets for a given ref gen.

        :param str genome: name of a reference genome assembly of interest
        :param str asset: name of particular asset to fetch
        :param str tag: name of particular tag to fetch
        :param bool unpack: whether to unpack a tarball
        :param bool | NoneType force: how to handle case in which asset path
            already exists; null for prompt (on a per-asset basis), False to
            effectively auto-reply No to the prompt to replace existing file,
            and True to auto-replay Yes for existing asset replacement.
        :param function(str, str, str) -> str get_json_url: how to build URL from
            genome server URL base, genome, and asset
        :param function(str) -> str get_main_url: how to get archive URL from
            main URL
        :param function(str) -> function build_signal_handler: how to create
            a signal handler to use during the download; the single argument
            to this function factory is the download filepath
        :return a pair of asset name and folder name (key-value pair with which genome config file
            is updated) if pull succeeds, else asset key and a null value.
        :raise refgenconf.UnboundEnvironmentVariablesError: if genome folder
            path contains any env. var. that's unbound
        """
        missing_vars = unbound_env_vars(self.genome_folder)
        if missing_vars:
            raise UnboundEnvironmentVariablesError(", ".join(missing_vars))

        def raise_unpack_error():
            raise NotImplementedError("Option to not extract tarballs is not yet supported.")

        tag = _download_json(get_json_url(self.genome_server, API_VERSION, genome, asset) + "/default_tag") \
            if tag is None else tag
        _LOGGER.debug("Determined tag: '{}'".format(tag))
        unpack or raise_unpack_error()

        url_attrs = get_json_url(self.genome_server, API_VERSION, genome, asset)
        url_archive = get_json_url(self.genome_server, API_VERSION, genome, asset) + "/archive"

        archive_data = _download_json(url_attrs, params={"tag": tag})

        if sys.version_info[0] == 2:
            archive_data = asciify_dict(archive_data)
        gat = [genome, asset, tag]
        # local directory that the asset data will be stored in
        tag_dir = os.path.dirname(self.filepath(*gat))
        # local directory the downloaded archive will be temporarily saved in
        genome_dir_path = os.path.join(self[CFG_FOLDER_KEY], genome)
        # local path to the temporarily saved archive
        filepath = os.path.join(genome_dir_path, asset + "__" + tag + ".tgz")
        # check if the genome/asset:tag exists and get request user decision
        if os.path.exists(tag_dir):
            def preserve():
                _LOGGER.debug("Preserving existing: {}".format(tag_dir))
                return asset, tag_dir

            def msg_overwrite():
                _LOGGER.debug("Overwriting: {}".format(tag_dir))
                shutil.rmtree(tag_dir)
            if force is False:
                return preserve()
            elif force is None:
                if not query_yes_no("Replace existing ({})?".format(tag_dir), "no"):
                    return preserve()
                else:
                    msg_overwrite()
            else:
                msg_overwrite()

        # check asset digests local-server match for each parent
        [self._check_asset_digest(genome, x) for x in archive_data[CFG_ASSET_PARENTS_KEY] if
         CFG_ASSET_PARENTS_KEY in archive_data]

        bundle_name = '{}/{}:{}'.format(*gat)
        archsize = archive_data[CFG_ARCHIVE_SIZE_KEY]
        _LOGGER.info("'{}' archive size: {}".format(bundle_name, archsize))
        if _is_large_archive(archsize) and not query_yes_no("Are you sure you want to download this large archive?"):
            _LOGGER.info("pull action aborted by user")
            return asset, None

        if not os.path.exists(genome_dir_path):
            _LOGGER.debug("Creating directory: {}".format(genome_dir_path))
            os.makedirs(genome_dir_path)

        # Download the file from `url` and save it locally under `filepath`:
        _LOGGER.info("Downloading URL: {}".format(url_archive))
        try:
            signal.signal(signal.SIGINT, build_signal_handler(filepath, self))
            _download_url_progress(url_archive, filepath, bundle_name, params={"tag": tag})
        except HTTPError as e:
            _LOGGER.error("File not found on server: {}".format(e))
            return asset, None
        except ConnectionRefusedError as e:
            _LOGGER.error(str(e))
            _LOGGER.error("Server {}/{} refused download. Check your internet settings".format(self.genome_server,
                                                                                               API_VERSION))
            return asset, None
        except ContentTooShortError as e:
            _LOGGER.error(str(e))
            _LOGGER.error("'{}' download incomplete".format(bundle_name))
            return asset, None
        else:
            _LOGGER.info("Download complete: {}".format(filepath))

        new_checksum = checksum(filepath)
        old_checksum = archive_data and archive_data.get(CFG_CHECKSUM_KEY)
        if old_checksum and new_checksum != old_checksum:
            _LOGGER.error("Checksum mismatch: ({}, {})".format(new_checksum, old_checksum))
            return asset, None
        else:
            _LOGGER.debug("Matched checksum: '{}'".format(old_checksum))
        import tempfile
        # successfully downloaded and moved tarball; untar it
        if unpack and filepath.endswith(".tgz"):
            _LOGGER.info("Extracting asset tarball and saving to: {}".format(tag_dir))
            tmpdir = tempfile.mkdtemp(dir=genome_dir_path)  # TODO: use context manager here when we drop support for py2
            untar(filepath, tmpdir)
            # here we suspect the unarchived asset to be an asset-named directory with the asset data inside
            # and we transfer it to the tag-named subdirectory
            shutil.move(os.path.join(tmpdir, asset), tag_dir)
            shutil.rmtree(tmpdir)
            if os.path.isfile(filepath):
                os.remove(filepath)
        self.update_tags(*gat, data={attr: archive_data[attr] for attr in ATTRS_COPY_PULL if attr in archive_data})
        self.set_default_pointer(*gat)
        self.write()
        return asset, archive_data[CFG_ASSET_PATH_KEY]

    def update_relatives_assets(self, genome, asset, tag=None, data=None, children=False):
        """
        A convenience method which wraps the update assets and uses it to update the asset relatives of an asset.

        :param str genome: genome to be added/updated
        :param str asset: asset to be added/updated
        :param str tag: tag to be added/updated
        :param list data: asset parents to be added/updated
        :param bool children: a logical indicating whether the relationship to be added is 'children'
        :return RefGenConf: updated object
        """
        def _extend_unique(l1, l2):
            """
            Extend a list with no duplicates

            :param list l1: original list
            :param list l2: list with items to add
            :return list: an extended list
            """
            return l1 + list(set(l2) - set(l1))
        tag = tag or self.get_default_tag(genome, asset)
        relationship = CFG_ASSET_CHILDREN_KEY if children else CFG_ASSET_PARENTS_KEY
        if _check_insert_data(data, list, "data"):
            self.update_tags(genome, asset, tag)  # creates/asserts the genome/asset:tag combination
            self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY][tag].setdefault(relationship, list())
            self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY][tag][relationship] = \
                _extend_unique(self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY][tag]
                               [relationship], data)

    def update_seek_keys(self, genome, asset, tag=None, keys=None):
        """
        A convenience method which wraps the update assets and uses it to update the seek keys for a tagged asset.

        :param str genome: genome to be added/updated
        :param str asset: asset to be added/updated
        :param str tag: tag to be added/updated
        :param Mapping keys: seek_keys to be added/updated
        :return RefGenConf: updated object
        """
        tag = tag or self.get_default_tag(genome, asset)
        if _check_insert_data(keys, Mapping, "keys"):
            self.update_tags(genome, asset, tag)  # creates/asserts the genome/asset:tag combination
            self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY][tag].setdefault(CFG_SEEK_KEYS_KEY,
                                                                                                     PXAM())
            self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY][tag][CFG_SEEK_KEYS_KEY].\
                update(keys)
        return self

    def update_tags(self, genome, asset=None, tag=None, data=None):
        """
        Updates the genomes in RefGenConf object at any level.
        If a requested genome-asset-tag mapping is missing, it will be created

        :param str genome: genome to be added/updated
        :param str asset: asset to be added/updated
        :param str tag: tag to be added/updated
        :param Mapping data: data to be added/updated
        :return RefGenConf: updated object
        """
        if _check_insert_data(genome, str, "genome"):
            self[CFG_GENOMES_KEY].setdefault(genome, PXAM())
            if _check_insert_data(asset, str, "asset"):
                self[CFG_GENOMES_KEY][genome].setdefault(CFG_ASSETS_KEY, PXAM())
                self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY].setdefault(asset, PXAM())
                if _check_insert_data(tag, str, "tag"):
                    self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset].setdefault(CFG_ASSET_TAGS_KEY, PXAM())
                    self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY].setdefault(tag, PXAM())
                    if _check_insert_data(data, Mapping, "data"):
                        self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY][tag].update(data)
        return self

    def update_assets(self, genome, asset=None, data=None):
        """
        Updates the genomes in RefGenConf object at any level.
        If a requested genome-asset mapping is missing, it will be created

        :param str genome: genome to be added/updated
        :param str asset: asset to be added/updated
        :param Mapping data: data to be added/updated
        :return RefGenConf: updated object
        """
        if _check_insert_data(genome, str, "genome"):
            self[CFG_GENOMES_KEY].setdefault(genome, PXAM())
            if _check_insert_data(asset, str, "asset"):
                self[CFG_GENOMES_KEY][genome].setdefault(CFG_ASSETS_KEY, PXAM())
                self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY].setdefault(asset, PXAM())
                if _check_insert_data(data, Mapping, "data"):
                    self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset].update(data)
        return self

    def remove_assets(self, genome, asset, tag=None):
        """
        Remove data associated with a specified genome:asset:tag combination.
        If no tags are specified, the entire asset is removed from the genome.

        If no more tags are defined for the selected genome:asset after tag removal,
        the parent asset will be removed as well
        If no more assets are defined for the selected genome after asset removal,
        the parent genome will be removed as well

        :param str genome: genome to be removed
        :param str asset: asset package to be removed
        :param str tag: tag to be removed
        :raise TypeError: if genome argument type is not a list or str
        :return RefGenConf: updated object
        """
        # TODO: add unit tests
        def _del_if_empty(obj, attr, alt=None):
            """
            Internal function for Mapping attribute deleting.
            Check if attribute exists and delete it if its length is zero.

            :param Mapping obj: an object to check
            :param str attr: Mapping attribute of interest
            :param list[Mapping, str] alt: a list of length 2 that indicates alternative
            Mapping-attribute combination to remove
            """
            if attr in obj and len(obj[attr]) == 0:
                if alt is None:
                    del obj[attr]
                else:
                    if alt[1] in alt[0]:
                        del alt[0][alt[1]]

        tag = tag or self.get_default_tag(genome, asset)
        if _check_insert_data(genome, str, "genome"):
            if _check_insert_data(asset, str, "asset"):
                if _check_insert_data(tag, str, "tag"):
                    del self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY][tag]
                    _del_if_empty(self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset], CFG_ASSET_TAGS_KEY,
                                  [self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY], asset])
                    _del_if_empty(self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY], asset)
                    _del_if_empty(self[CFG_GENOMES_KEY][genome], CFG_ASSETS_KEY, [self[CFG_GENOMES_KEY], genome])
                    _del_if_empty(self[CFG_GENOMES_KEY], genome)
                    try:
                        default_tag = self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_DEFAULT_TAG_KEY]
                    except KeyError:
                        pass
                    else:
                        if default_tag == tag:
                            del self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_DEFAULT_TAG_KEY]
                    if len(self[CFG_GENOMES_KEY]) == 0:
                        self[CFG_GENOMES_KEY] = None
        return self

    def update_genomes(self, genome, data=None):
        """
        Updates the genomes in RefGenConf object at any level.
        If a requested genome is missing, it will be added

        :param str genome: genome to be added/updated
        :param Mapping data: data to be added/updated
        :return RefGenConf: updated object
        """
        if _check_insert_data(genome, str, "genome"):
            self[CFG_GENOMES_KEY].setdefault(genome, PXAM({CFG_ASSETS_KEY: PXAM()}))
            if _check_insert_data(data, Mapping, "data"):
                self[CFG_GENOMES_KEY][genome].update(data)
        return self

    def get_genome_attributes(self, genome):
        """
        Get the dictionary attributes, like checksum, contents, description. Does not return the assets.

        :param str genome: genome to get the attributes dict for
        :return Mapping[str, str]: available genome attributes
        """
        return {k: self[CFG_GENOMES_KEY][genome][k]
                for k in CFG_GENOME_ATTRS_KEYS if k in self[CFG_GENOMES_KEY][genome]}

    def is_asset_complete(self, genome, asset, tag):
        """
        Check whether all required tag attributes are defined in the RefGenConf object.
        This is the way we determine tag completeness.

        :param str genome: genome to be checked
        :param str asset: asset package to be checked
        :param str tag: tag to be checked
        :return bool: the decision
        """
        tag_data = self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY][tag]
        return all([r in tag_data for r in REQ_TAG_ATTRS])

    def _invert_genomes(self, order=None):
        """ Map each asset type/kind/name to a collection of assemblies.

        A configuration file encodes assets by genome, but in some use cases
        it's helpful to invert the direction of this mapping. The value of the
        asset key/name may differ by genome, so that information is
        necessarily lost in this inversion, but we can collect genome IDs by
        asset ID.

        :param order: function(str) -> object how to key genome IDs and asset
            names for sort
        :return OrderedDict[str, Iterable[str]] binding between asset kind/key/name
            and collection of reference genome assembly names for which the
            asset type is available
        """
        genomes = {}
        for g, am in self[CFG_GENOMES_KEY].items():
            for a in am[CFG_ASSETS_KEY].keys():
                genomes.setdefault(a, []).append(g)
        assets = sorted(genomes.keys(), key=order)
        return OrderedDict([(a, sorted(genomes[a], key=order)) for a in assets])

    def _check_asset_digest(self, genome, remote_asset_name):
        """
        Check local asset digest against the remote one. In case the local asset does not exist,
        the config is populated with the remote asset digest data

        :param str genome: name of the genome to check the asset digests for
        :param str remote_asset_name: asset and tag names, formatted like: asset:tag
        :raise KeyError: if the local digest does not match its remote counterpart
        """
        remote_asset_data = prp(remote_asset_name)
        asset = remote_asset_data["item"]
        tag = remote_asset_data["tag"]
        asset_digest_url = "{}/{}/asset/{}/{}/{}/asset_digest".\
            format(self.genome_server, API_VERSION, genome, asset, tag)
        remote_digest = _download_json(asset_digest_url)
        try:
            # we need to allow for missing seek_keys section so that the digest is respected even from the previously
            # populated just asset_digest metadata from the server
            _assert_gat_exists(self[CFG_GENOMES_KEY], genome, asset, tag,
                               allow_incomplete=not self.is_asset_complete(genome, asset, tag))
        except (KeyError, MissingAssetError, MissingGenomeError, MissingSeekKeyError):
            self.update_tags(genome, asset, tag, {CFG_ASSET_CHECKSUM_KEY: remote_digest})
            _LOGGER.info("Could not find '{}/{}:{}' digest. Populating with server data".format(genome, asset, tag))
        else:
            local_digest = self[CFG_GENOMES_KEY][genome][CFG_ASSETS_KEY][asset][CFG_ASSET_TAGS_KEY]\
                [tag][CFG_ASSET_CHECKSUM_KEY]
            if remote_digest != local_digest:
                msg = "This asset is built from parent asset '{}', but for this parent, the remote does not "\
                    "match your local asset (local: {}; remote: {}). Refgenie will not pull this asset "\
                    "because the remote version was not built from the same parent asset you have locally."\
                    .format(asset, local_digest, remote_digest)
                _LOGGER.error(msg)
                raise RefgenconfError(msg)


class DownloadProgressBar(tqdm):
    """
    from: https://github.com/tqdm/tqdm#hooks-and-callbacks
    """
    def update_to(self, b=1, bsize=1, tsize=None):
        """
        Update the progress bar

        :param int b: number of blocks transferred so far
        :param int bsize: size of each block (in tqdm units)
        :param int tsize: total size (in tqdm units)
        """
        if tsize is not None:
            self.total = tsize
        self.update(b * bsize - self.n)


def _download_json(url, params=None):
    """
    Safely connect to the provided API endpoint and download JSON data.

    :param str url: server API endpoint
    :param dict params: query parameters
    :return dict: served data
    """
    import requests
    _LOGGER.debug("Downloading JSON data; querying URL: '{}'".format(url))
    resp = requests.get(url, params=params)
    if resp.ok:
        return resp.json()
    raise DownloadJsonError(resp)


def _download_url_progress(url, output_path, name, params=None):
    """
    Download asset at given URL to given filepath, show progress along the way.

    :param str url: server API endpoint
    :param str output_path: path to file to save download
    :param str name: name to display in front of the progress bar
    :param dict params: query parameters to be added to the request
    """
    url = url if params is None else url + "?{}".format(urllib.parse.urlencode(params))
    with DownloadProgressBar(unit_scale=True, desc=name, unit="B") as dpb:
        urllib.request.urlretrieve(url, filename=output_path, reporthook=dpb.update_to)


def _genome_asset_path(genomes, gname, aname, tname, seek_key, enclosing_dir):
    """
    Retrieve the raw path value for a particular asset for a particular genome.

    :param Mapping[str, Mapping[str, Mapping[str, object]]] genomes: nested
        collection of key-value pairs, keyed at top level on genome ID, then by
        asset name, then by asset attribute
    :param str gname: top level key to query -- genome ID, e.g. mm10
    :param str aname: second-level key to query -- asset name, e.g. fasta
    :param str tname: third-level key to query -- tag name, e.g. default
    :param str seek_key: fourth-level key to query -- tag name, e.g. chrom_sizes
    :param bool enclosing_dir: whether a path to the entire enclosing directory should be returned, e.g.
        for a fasta asset that has 3 seek_keys pointing to 3 files in an asset dir, that asset dir is returned
    :return str: raw path value for a particular asset for a particular genome
    :raise MissingGenomeError: if the given key-value pair collection does not
        contain as a top-level key the given genome ID
    :raise MissingAssetError: if the given key-value pair colelction does
        contain the given genome ID, but that key's mapping doesn't contain
        the given asset name as a key
    :raise GenomeConfigFormatError: if it's discovered during the query that
        the structure of the given genomes mapping suggests that it was
        parsed from an improperly formatted/structured genome config file.
    """
    _assert_gat_exists(genomes, gname, aname, tname)
    asset_tag_data = genomes[gname][CFG_ASSETS_KEY][aname][CFG_ASSET_TAGS_KEY][tname]
    if enclosing_dir:
        return os.path.join(asset_tag_data[CFG_ASSET_PATH_KEY], tname)
    if seek_key is None:
        if aname in asset_tag_data[CFG_SEEK_KEYS_KEY]:
            seek_key = aname
        else:
            return os.path.join(asset_tag_data[CFG_ASSET_PATH_KEY], tname)
    try:
        seek_key_value = asset_tag_data[CFG_SEEK_KEYS_KEY][seek_key]
        appendix = "" if seek_key_value == "." else seek_key_value
        return os.path.join(asset_tag_data[CFG_ASSET_PATH_KEY], tname, appendix)
    except KeyError:
        raise MissingSeekKeyError("genome/asset:tag bundle '{}/{}:{}' exists, but seek_key '{}' is missing".
                                  format(gname, aname, tname, seek_key))


def _assert_gat_exists(genomes, gname, aname=None, tname=None, allow_incomplete=False):
    """
    Make sure the genome/asset:tag combination exists in the provided mapping and has any seek keys defined.
    Seek keys are required for the asset completeness.

    :param Mapping[str, Mapping[str, Mapping[str, object]]] genomes: nested
        collection of key-value pairs, keyed at top level on genome ID, then by
        asset name, then by asset attribute
    :param str gname: top level key to query -- genome ID, e.g. mm10
    :param str aname: second-level key to query -- asset name, e.g. fasta
    :param str tname: third-level key to query -- tag name, e.g. default
    :raise MissingGenomeError: if the given key-value pair collection does not
        contain as a top-level key the given genome ID
    :raise MissingAssetError: if the given key-value pair collection does
        contain the given genome ID, but that key's mapping doesn't contain
        the given asset name as a key
    :raise GenomeConfigFormatError: if it's discovered during the query that
        the structure of the given genomes mapping suggests that it was
        parsed from an improperly formatted/structured genome config file.
    """
    _LOGGER.debug("checking existence of: {}/{}:{}".format(gname, aname, tname))
    try:
        genome = genomes[gname]
    except KeyError:
        raise MissingGenomeError("Your genomes do not include {}".format(gname))
    if aname is not None:
        try:
            asset_data = genome[CFG_ASSETS_KEY][aname]
        except KeyError:
            raise MissingAssetError("Genome '{}' exists, but asset '{}' is missing".format(gname, aname))
        if tname is not None:
            try:
                tag_data = asset_data[CFG_ASSET_TAGS_KEY][tname]
            except KeyError:
                raise MissingTagError(
                    "genome/asset bundle '{}/{}' exists, but tag '{}' is missing".format(gname, aname, tname))
            try:
                tag_data[CFG_SEEK_KEYS_KEY]
            except KeyError:
                if not allow_incomplete:
                    raise MissingSeekKeyError("Asset incomplete. No seek keys are defined for '{}/{}:{}'. "
                                              "Build or pull the asset again.".format(gname, aname, tname))


def _is_large_archive(size):
    """
    Determines if the file is large based on a string formatted as follows: 15.4GB

    :param str size:  size string
    :return bool: the decision
    """
    _LOGGER.debug("Checking archive size: '{}'".format(size))
    return size.endswith("TB") or (size.endswith("GB") and float("".join(c for c in size if c in '0123456789.')) > 5)


def _list_remote(url, genome, order=None):
    """
    List genomes and assets available remotely.

    :param url: location or ref genome config data
    :param order: function(str) -> object how to key genome IDs and asset
        names for sort
    :return str, str: text reps of remotely available genomes and assets
    """
    genomes_data = _read_remote_data(url)
    refgens = _select_genomes(sorted(genomes_data.keys(), key=order), genome)
    filtered_genomes_data = {refgen: genomes_data[refgen] for refgen in refgens}
    asset_texts = ["{}/   {}".format(g.rjust(20), ", ".join(a)) for g, a in filtered_genomes_data.items()]
    return ", ".join(refgens), "\n".join(asset_texts)


def _make_genome_assets_line(gen, assets, offset_text="  ", genome_assets_delim="/ ", asset_sep=", ", order=None,
                             asset_tag_delim=":"):
    """
    Build a line of text for display of assets by genome

    :param str gen: reference assembly ID, e.g. hg38
    :param Iterable[str] assets: collection of asset names for the given genome
    :param str offset_text: prefix for the line, e.g. a kind of whitespace
    :param str genome_assets_delim: delimiter between a genome ID and text
        showing names of assets for that genome
    :param str asset_sep: delimiter between asset names
    :param order: function(str) -> object how to key asset names for sort
    :return str: text representation of a single assembly's name and assets
    """
    tagged_assets = asset_sep.join(sorted(_make_asset_tags_product(assets, asset_tag_delim), key=order))
    return "{}{}{}{}".format(gen.rjust(20), genome_assets_delim, offset_text, tagged_assets)


def _make_asset_tags_product(assets, asset_tag_delim=":", asset_sk_delim="."):
    """
    Make a product of assets and tags available in the provided mapping

    :param Mapping assets: the assets for a selected genome
    :param str asset_tag_delim: how to represent the asset-tag link
    :param str asset_sk_delim: how to represent the asset-seek_key link
    :return list: list representation of tagged assets
    """
    tagged_assets = []
    for aname, asset in assets.items():
        for tname, tag in asset[CFG_ASSET_TAGS_KEY].items():
            sk_assets = []
            seek_keys = get_tag_seek_keys(tag)
            # proceed only if asset is 'complete' -- has seek_keys
            if seek_keys is not None:
                # add seek_keys if exist and different from the asset name, otherwise just the asset name
                sk_assets.extend([asset_sk_delim.join([aname, sk]) if sk != aname else aname for sk in seek_keys])
            # add tags to the asset.seek_key list
            tagged_assets.extend([asset_tag_delim.join(i) for i in itertools.product(sk_assets, [tname])])
    return tagged_assets


def _read_remote_data(url):
    """
    Read as JSON data from a URL request response.

    :param str url: data request
    :return dict: JSON parsed from the response from given URL request
    """
    import json
    with urllib.request.urlopen(url) as response:
        encoding = response.info().get_content_charset('utf8')
        return json.loads(response.read().decode(encoding))


def _check_insert_data(obj, datatype, name):
    """ Checks validity of an object """
    if obj is None:
        return False
    if not isinstance(obj, datatype):
        raise TypeError("{} must be {}; got {}".format(
            name, datatype.__name__, type(obj).__name__))
    return True


def _select_genomes(genomes, genome=None):
    """
    Safely select a subset of genomes

    :param list[str] | str genome: genomes that the assets should be found for
    :raise TypeError: if genome argument type is not a list or str
    :return list: selected subset of genomes
    """
    if genome:
        if isinstance(genome, str):
            genome = [genome]
        elif not isinstance(genome, list) or not all(isinstance(i, str) for i in genome):
            raise TypeError("genome has to be a list[str] or a str, got '{}'".format(genome.__class__.__name__))
    return genomes if (genome is None or not all(x in genomes for x in genome)) else genome


def get_asset_tags(asset):
    """
    Return a list of asset tags.

    These need an accession function since under the tag name key there are not only tag names, but also the
     default tag pointer

    :param Mapping asset: a single asset part of the RefGenConf
    :return list: asset tags
    """
    return [t for t in asset[CFG_ASSET_TAGS_KEY]]


def get_tag_seek_keys(tag):
    """
    Return a list of tag seek keys.

    :param Mapping tag: a single tag part of the RefGenConf
    :return list: tag seek keys
    """
    return [s for s in tag[CFG_SEEK_KEYS_KEY]] if CFG_SEEK_KEYS_KEY in tag else None
