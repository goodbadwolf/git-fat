import os
import subprocess
import threading
import hashlib
import tempfile
import logging
import sys
from . import utils


class GitFat:
    """Main class for git-fat operations"""

    DecodeError = RuntimeError

    def __init__(self):
        self.gitroot = utils.get_git_root()
        self.gitdir = utils.get_git_dir()
        self.objdir = os.path.join(self.gitdir, "fat", "objects")
        self.magiclen = self._calculate_magic_len()
        self.magiclens = self._calculate_all_magic_lens()

    def _calculate_magic_len(self):
        """Calculate the length of encoded fat object reference"""
        dummy_hash = hashlib.sha1("dummy".encode("utf-8")).hexdigest()
        return len(self.encode(dummy_hash, 5))

    def _calculate_all_magic_lens(self):
        """Calculate lengths for all encoding versions"""
        encoders = [self.encode_v1, self.encode_v2]
        return [
            len(enc(hashlib.sha1("dummy".encode("utf-8")).hexdigest(), 5))
            for enc in encoders
        ]

    def setup(self):
        """Initialize git-fat directory structure"""
        utils.mkdir_p(self.objdir)

    def encode_v1(self, digest, bytes):
        """Legacy encoding for fat object references"""
        return "#$# git-fat %s\n" % (digest,)

    def encode_v2(self, digest, bytes):
        """Current encoding for fat object references"""
        return "#$# git-fat %s %20d\n" % (digest, bytes)

    def encode(self, digest, bytes):
        """Encode fat object reference using current version"""
        if os.environ.get("GIT_FAT_VERSION") == "1":
            return self.encode_v1(digest, bytes)
        return self.encode_v2(digest, bytes)

    def decode(self, string, noraise=False):
        """Decode fat object reference"""
        cookie = "#$# git-fat "
        if string.startswith(cookie):
            parts = string[len(cookie) :].split()
            digest = parts[0]
            bytes = int(parts[1]) if len(parts) > 1 else None
            return digest, bytes
        elif noraise:
            return None, None
        else:
            raise self.DecodeError("Could not decode %s" % (string))

    def is_init_done(self):
        return utils.gitconfig_get("filter.fat.clean") or utils.gitconfig_get(
            "filter.fat.smudge"
        )

    def assert_init_done(self):
        if not self.is_init_done():
            logging.error("git-fat is not yet configured in this repository.\n")
            logging.info('Run "git fat init" to configure.\n')
            sys.exit(1)

    def get_rsync(self):
        cfgpath = os.path.join(self.gitroot, ".gitfat")
        remote = utils.gitconfig_get("rsync.remote", file=cfgpath)
        ssh_port = utils.gitconfig_get("rsync.sshport", file=cfgpath)
        ssh_user = utils.gitconfig_get("rsync.sshuser", file=cfgpath)
        options = utils.gitconfig_get("rsync.options", file=cfgpath)
        if remote is None:
            raise RuntimeError("No rsync.remote in %s" % cfgpath)
        return remote, ssh_port, ssh_user, options

    def get_rsync_command(self, push):
        (remote, ssh_port, ssh_user, options) = self.get_rsync()
        if push:
            logger.debug("Pushing to %s" % (remote))
        else:
            logger.debug("Pulling from %s" % (remote))

        cmd = ["rsync", "--progress", "--ignore-existing", "--from0", "--files-from=-"]
        rshopts = ""
        if ssh_user:
            rshopts += " -l " + ssh_user
        if ssh_port:
            rshopts += " -p " + ssh_port
        if rshopts:
            cmd.append("--rsh=ssh" + rshopts)
        if options:
            cmd += options.split(" ")
        if push:
            cmd += [self.objdir + "/", remote + "/"]
        else:
            cmd += [remote + "/", self.objdir + "/"]
        return cmd

    def revparse(self, revname):
        return subprocess.check_output(["git", "rev-parse", revname]).strip()

    def encode_v1(self, digest, bytes):
        "Produce legacy representation of file to be stored in repository."
        return "#$# git-fat %s\n" % (digest,)

    def encode_v2(self, digest, bytes):
        "Produce representation of file to be stored in repository. 20 characters can hold 64-bit integers."
        return "#$# git-fat %s %20d\n" % (digest, bytes)

    def decode(self, string, noraise=False):
        cookie = "#$# git-fat "
        if string.startswith(cookie):
            parts = string[len(cookie) :].split()
            digest = parts[0]
            bytes = int(parts[1]) if len(parts) > 1 else None
            return digest, bytes
        elif noraise:
            return None, None
        else:
            raise GitFat.DecodeError("Could not decode %s" % (string))

    def decode_stream(self, stream):
        "Return digest if git-fat cache, otherwise return iterator over entire file contents"
        preamble = stream.read(self.magiclen)
        try:
            return self.decode(preamble)
        except GitFat.DecodeError:
            # Not sure if this is the right behavior
            return itertools.chain([preamble], readblocks(stream)), None

    def decode_file(self, fname):
        # Fast check
        try:
            stat = os.lstat(fname)
        except OSError:
            return False, None
        if stat.st_size != self.magiclen:
            return False, None
        # read file
        try:
            digest, bytes = self.decode_stream(open(fname))
        except IOError:
            return False, None
        if isinstance(digest, str):
            return digest, bytes
        else:
            return None, bytes

    def decode_clean(self, body):
        """
        Attempt to decode version in working tree. The tree version could be changed to have a more
        useful message than the machine-readable copy that goes into the repository. If the tree
        version decodes successfully, it indicates that the fat data is not currently available in
        this repository.
        """
        digest, bytes = self.decode(body, noraise=True)
        return digest

    def filter_clean(self, instream, outstreamclean):
        h = hashlib.new("sha1")
        bytes = 0
        fd, tmpname = tempfile.mkstemp(dir=self.objdir)
        try:
            ishanging = False
            cached = False  # changes to True when file is cached
            with os.fdopen(fd, "w") as cache:
                outstream = cache
                firstblock = True
                for block in readblocks(instream):
                    if firstblock:
                        if len(block) == self.magiclen and self.decode_clean(
                            block[0 : self.magiclen]
                        ):
                            ishanging = True  # Working tree version is verbatim from repository (not smudged)
                            outstream = outstreamclean
                        firstblock = False
                    h.update(block.encode("utf-8"))
                    bytes += len(block)
                    outstream.write(block)
                outstream.flush()
            digest = h.hexdigest()
            objfile = os.path.join(self.objdir, digest)
            if not ishanging:
                if os.path.exists(objfile):
                    logging.debug(
                        "git-fat filter-clean: cache already exists %s" % objfile
                    )
                    os.remove(tmpname)
                else:
                    # Set permissions for the new file using the current umask
                    os.chmod(tmpname, int("444", 8) & ~umask())
                    os.rename(tmpname, objfile)
                    logger.debug("git-fat filter-clean: caching to %s" % objfile)
                cached = True
                outstreamclean.write(self.encode(digest, bytes))
        finally:
            if not cached:
                os.remove(tmpname)

    def cmd_filter_clean(self):
        """
        The clean filter runs when a file is added to the index. It gets the "smudged" (tree)
        version of the file on stdin and produces the "clean" (repository) version on stdout.
        """
        self.setup()
        self.filter_clean(sys.stdin, sys.stdout)

    def cmd_filter_smudge(self):
        self.setup()
        result, bytes = self.decode_stream(sys.stdin)
        if isinstance(result, str):  # We got a digest
            objfile = os.path.join(self.objdir, result)
            try:
                cat(open(objfile), sys.stdout)
                logger.debug("git-fat filter-smudge: restoring from %s" % objfile)
            except IOError:  # file not found
                logger.debug("git-fat filter-smudge: fat object missing %s" % objfile)
                sys.stdout.write(
                    self.encode(result, bytes)
                )  # could leave a better notice about how to recover this file
        else:  # We have an iterable over the original input.
            logger.debug("git-fat filter-smudge: not a managed file")
            cat_iter(result, sys.stdout)

    def catalog_objects(self):
        return set(os.listdir(self.objdir))

    def referenced_objects(self, rev=None, all=False):
        referenced = set()
        if all:
            rev = "--all"
        elif rev is None:
            rev = self.revparse("HEAD")
        # Revision list gives us object names to inspect with cat-file...
        p1 = subprocess.Popen(
            ["git", "rev-list", "--objects", rev], stdout=subprocess.PIPE
        )

        def cut_sha1hash(input, output):
            for line in input:
                line = line.decode("utf-8")
                to_write = line.split()[0] + "\n"
                to_write = to_write.encode("utf-8")
                output.write(to_write)
            output.close()

        # ...`cat-file --batch-check` filters for git-fat object candidates in bulk...
        p2 = subprocess.Popen(
            ["git", "cat-file", "--batch-check"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

        def filter_gitfat_candidates(input, output):
            for line in input:
                objhash, objtype, size = line.decode("utf-8").split()
                if objtype == "blob" and int(size) in self.magiclens:
                    to_write = objhash + "\n"
                    to_write = to_write.encode("utf-8")
                    output.write(to_write)
            output.close()

        # ...`cat-file --batch` provides full contents of git-fat candidates in bulk
        p3 = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        # Stream data: p1 | cut_thread | p2 | filter_thread | p3
        cut_thread = threading.Thread(target=cut_sha1hash, args=(p1.stdout, p2.stdin))
        filter_thread = threading.Thread(
            target=filter_gitfat_candidates, args=(p2.stdout, p3.stdin)
        )
        cut_thread.start()
        filter_thread.start()
        # Process metadata + content format provided by `cat-file --batch`
        while True:
            metadata_line = p3.stdout.readline()
            if not metadata_line:
                break  # EOF
            metadata_line = metadata_line.decode("utf-8")
            objhash, objtype, size_str = metadata_line.split()
            size, bytes_read = int(size_str), 0
            # We know from filter that item is a candidate git-fat object and
            # is small enough to read into memory and process
            content = ""
            while bytes_read < size:
                data = p3.stdout.read(size - bytes_read)
                if not data:
                    break  # EOF
                data = data.decode("utf-8")
                content += data
                bytes_read += len(data)
            try:
                fathash = self.decode(content)[0]
                referenced.add(fathash)
            except GitFat.DecodeError:
                pass
            # Consume LF record delimiter in `cat-file --batch` output
            bytes_read = 0
            while bytes_read < 1:
                data = p3.stdout.read(1)
                if not data:
                    break  # EOF
                bytes_read += len(data)
        # Ensure everything is cleaned up
        cut_thread.join()
        filter_thread.join()
        p1.wait()
        p2.wait()
        p3.wait()
        return referenced

    def orphan_files(self, patterns=[]):
        "generator for all orphan placeholders in the working tree"
        if not patterns or patterns == [""]:
            patterns = ["."]
        for fname in (
            subprocess.check_output(["git", "ls-files", "-z"] + patterns)
            .decode("utf-8")
            .split("\x00")[:-1]
        ):
            digest = self.decode_file(fname)[0]
            if digest:
                yield (digest, fname)

    def cmd_status(self, args):
        self.setup()
        catalog = self.catalog_objects()
        refargs = dict()
        if "--all" in args:
            refargs["all"] = True
        referenced = self.referenced_objects(**refargs)
        garbage = catalog - referenced
        orphans = referenced - catalog
        if "--all" in args:
            for obj in referenced:
                logger.info(obj)
        if orphans:
            logger.info("Orphan objects:")
            for orph in orphans:
                logger.info("    " + orph)
        if garbage:
            logger.info("Garbage objects:")
            for g in garbage:
                logger.info("    " + g)

    def is_dirty(self):
        return subprocess.call(["git", "diff-index", "--quiet", "HEAD"]) == 0

    def cmd_push(self, args):
        "Push anything that I have stored and referenced"
        self.setup()
        # Default to push only those objects referenced by current HEAD
        # (includes history). Finer-grained pushing would be useful.
        pushall = "--all" in args
        files = self.referenced_objects(all=pushall) & self.catalog_objects()
        cmd = self.get_rsync_command(push=True)
        logger.debug("Executing: %s" % " ".join(cmd))
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        p.communicate(input=b"\x00".join((f.encode("utf-8") for f in files)))
        if p.returncode:
            sys.exit(p.returncode)

    def checkout(self, show_orphans=False):
        "Update any stale files in the present working tree"
        self.assert_init_done()
        for digest, fname in self.orphan_files():
            objpath = os.path.join(self.objdir, digest)
            if os.access(objpath, os.R_OK):
                logger.info("Restoring %s -> %s" % (digest, fname))
                # The output of our smudge filter depends on the existence of
                # the file in .git/fat/objects, but git caches the file stat
                # from the previous time the file was smudged, therefore it
                # won't try to re-smudge. I don't know a git command that
                # specifically invalidates that cache, but changing the mtime
                # on the file will invalidate the cache.
                # Here we set the mtime to mtime + 1. This is an improvement
                # over touching the file as it catches the edgecase where a
                # git-checkout happens within the same second as a git fat
                # checkout.
                stat = os.lstat(fname)
                os.utime(fname, (stat.st_atime, stat.st_mtime + 1))
                # This re-smudge is essentially a copy that restores
                # permissions.
                subprocess.check_call(
                    ["git", "checkout-index", "--index", "--force", fname]
                )
            elif show_orphans:
                logger.info("Data unavailable: %s %s" % (digest, fname))

    def cmd_pull(self, args):
        "Pull anything that I have referenced, but not stored"
        self.setup()
        refargs = dict()
        if "--all" in args:
            refargs["all"] = True
        for arg in args:
            if arg.startswith("-") or len(arg) != 40:
                continue
            rev = self.revparse(arg)
            if rev:
                refargs["rev"] = rev
        files = self.filter_objects(refargs, self.parse_pull_patterns(args))
        files = (f.encode("utf-8") for f in files)
        cmd = self.get_rsync_command(push=False)
        logger.debug("Executing: %s" % " ".join(cmd))
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        p.communicate(input=b"\x00".join(files))
        if p.returncode:
            sys.exit(p.returncode)
        self.checkout()

    def parse_pull_patterns(self, args):
        if "--" not in args:
            return [""]
        else:
            idx = args.index("--")
            patterns = args[idx + 1 :]  # we don't care about '--'
            return patterns

    def filter_objects(self, refargs, patterns):
        files = self.referenced_objects(**refargs) - self.catalog_objects()
        if refargs.get(
            "all"
        ):  # Currently ignores patterns; can we efficiently do both?
            return files
        orphans_matched = list(self.orphan_files(patterns))
        orphans_objects = set(map(lambda x: x[0], orphans_matched))
        return files & orphans_objects

    def cmd_checkout(self, args):
        self.checkout(show_orphans=True)

    def cmd_gc(self):
        garbage = self.catalog_objects() - self.referenced_objects()
        logger.info("Unreferenced objects to remove: %d" % len(garbage))
        for obj in garbage:
            fname = os.path.join(self.objdir, obj)
            logger.info("%10d %s" % (os.stat(fname).st_size, obj))
            os.remove(fname)

    def cmd_verify(self):
        """Print details of git-fat objects with incorrect data hash"""
        corrupted_objects = []
        for obj in self.catalog_objects():
            fname = os.path.join(self.objdir, obj)
            h = hashlib.new("sha1")
            for block in readblocks(open(fname)):
                h.update(block.encode("utf-8"))
            data_hash = h.hexdigest()
            if obj != data_hash:
                corrupted_objects.append((obj, data_hash))
        if corrupted_objects:
            logger.info("Corrupted objects: %d" % len(corrupted_objects))
            for obj, data_hash in corrupted_objects:
                logger.info("%s data hash is %s" % (obj, data_hash))
            sys.exit(1)

    def cmd_init(self):
        self.setup()
        if self.is_init_done():
            logger.info(
                "Git fat already configured, check configuration in .git/config"
            )
        else:
            gitconfig_set("filter.fat.clean", "git-fat filter-clean")
            gitconfig_set("filter.fat.smudge", "git-fat filter-smudge")
            logger.info("Initialized git fat")

    def gen_large_blobs(self, revs, threshsize):
        """Build dict of all blobs"""
        time0 = time.time()

        def hash_only(input, output):
            """The output of git rev-list --objects shows extra info for blobs, subdirectory trees, and tags.
            This truncates to one hash per line.
            """
            for line in input:
                output.write(line[:40] + "\n")
            output.close()

        revlist = subprocess.Popen(
            ["git", "rev-list", "--all", "--objects"],
            stdout=subprocess.PIPE,
            bufsize=-1,
        )
        objcheck = subprocess.Popen(
            ["git", "cat-file", "--batch-check"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=-1,
        )
        hashonly = threading.Thread(
            target=hash_only, args=(revlist.stdout, objcheck.stdin)
        )
        hashonly.start()
        numblobs = 0
        numlarge = 1
        # Build dict with the sizes of all large blobs
        for line in objcheck.stdout:
            objhash, blob, size = line.split()
            if blob != "blob":
                continue
            size = int(size)
            numblobs += 1
            if size > threshsize:
                numlarge += 1
                yield objhash, size
        revlist.wait()
        objcheck.wait()
        hashonly.join()
        time1 = time.time()
        logger.debug(
            "%d of %d blobs are >= %d bytes [elapsed %.3fs]"
            % (numlarge, numblobs, threshsize, time1 - time0)
        )

    def cmd_find(self, args):
        maxsize = int(args[0])
        blobsizes = dict(self.gen_large_blobs("--all", maxsize))
        time0 = time.time()
        # Find all names assumed by large blobs (those in blobsizes)
        pathsizes = collections.defaultdict(lambda: set())
        revlist = subprocess.Popen(
            ["git", "rev-list", "--all"], stdout=subprocess.PIPE, bufsize=-1
        )
        difftree = subprocess.Popen(
            [
                "git",
                "diff-tree",
                "--root",
                "--no-renames",
                "--no-commit-id",
                "--diff-filter=AMCR",
                "-r",
                "--stdin",
                "-z",
            ],
            stdin=revlist.stdout,
            stdout=subprocess.PIPE,
        )
        for newblob, modflag, path in difftreez_reader(difftree.stdout):
            bsize = blobsizes.get(newblob)
            if bsize:  # We care about this blob
                pathsizes[path].add(bsize)
        time1 = time.time()
        logger.debug("Found %d paths in %.3f s" % (len(pathsizes), time1 - time0))
        maxlen = max(map(len, pathsizes)) if pathsizes else 0
        for path, sizes in sorted(
            pathsizes.items(), key=lambda ps: max(ps[1]), reverse=True
        ):
            logger.info(
                "%-*s filter=fat -text # %10d %d"
                % (maxlen, path, max(sizes), len(sizes))
            )
        revlist.wait()
        difftree.wait()

    def cmd_index_filter(self, args):
        manage_gitattributes = "--manage-gitattributes" in args
        filelist = set(f.strip() for f in open(args[0]).readlines())
        lsfiles = subprocess.Popen(["git", "ls-files", "-s"], stdout=subprocess.PIPE)
        updateindex = subprocess.Popen(
            ["git", "update-index", "--index-info"], stdin=subprocess.PIPE
        )
        for line in lsfiles.stdout:
            mode, sep, tail = line.partition(" ")
            blobhash, sep, tail = tail.partition(" ")
            stageno, sep, tail = tail.partition("\t")
            filename = tail.strip()
            if filename not in filelist:
                continue
            if mode == "120000":
                # skip symbolic links
                continue
            # This file will contain the hash of the cleaned object
            hashfile = os.path.join(self.gitdir, "fat", "index-filter", blobhash)
            try:
                cleanedobj = open(hashfile).read().rstrip()
            except IOError:
                catfile = subprocess.Popen(
                    ["git", "cat-file", "blob", blobhash], stdout=subprocess.PIPE
                )
                hashobject = subprocess.Popen(
                    ["git", "hash-object", "-w", "--stdin"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                )

                def dofilter():
                    self.filter_clean(catfile.stdout, hashobject.stdin)
                    hashobject.stdin.close()

                filterclean = threading.Thread(target=dofilter)
                filterclean.start()
                cleanedobj = hashobject.stdout.read().rstrip()
                catfile.wait()
                hashobject.wait()
                filterclean.join()
                mkdir_p(os.path.dirname(hashfile))
                open(hashfile, "w").write(cleanedobj + "\n")
            updateindex.stdin.write(
                "%s %s %s\t%s\n" % (mode, cleanedobj, stageno, filename)
            )
        if manage_gitattributes:
            try:
                mode, blobsha1, stageno, filename = subprocess.check_output(
                    ["git", "ls-files", "-s", ".gitattributes"]
                ).split()
                gitattributes_lines = subprocess.check_output(
                    ["git", "cat-file", "blob", blobsha1]
                ).splitlines()
            except ValueError:  # Nothing to unpack, thus no file
                mode, stageno = "100644", "0"
                gitattributes_lines = []
            gitattributes_extra = [
                "%s filter=fat -text" % line.split()[0] for line in filelist
            ]
            hashobject = subprocess.Popen(
                ["git", "hash-object", "-w", "--stdin"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            stdout, stderr = hashobject.communicate(
                "\n".join(gitattributes_lines + gitattributes_extra) + "\n"
            )
            updateindex.stdin.write(
                "%s %s %s\t%s\n" % (mode, stdout.strip(), stageno, ".gitattributes")
            )
        updateindex.stdin.close()
        lsfiles.wait()
        updateindex.wait()
