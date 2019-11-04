#!/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Usage: symbolstore.py <params> <dump_syms path> <symbol store path>
#                                <debug info files or dirs>
#   Runs dump_syms on each debug info file specified on the command line,
#   then places the resulting symbol file in the proper directory
#   structure in the symbol store path.  Accepts multiple files
#   on the command line, so can be called as part of a pipe using
#   find <dir> | xargs symbolstore.pl <dump_syms> <storepath>
#   But really, you might just want to pass it <dir>.
#
#   Parameters accepted:
#     -c           : Copy debug info files to the same directory structure
#                    as sym files. On Windows, this will also copy
#                    binaries into the symbol store.
#     -a "<archs>" : Run dump_syms -a <arch> for each space separated
#                    cpu architecture in <archs> (only on OS X)
#     -s <srcdir>  : Use <srcdir> as the top source directory to
#                    generate relative filenames.

from __future__ import print_function

import errno
import sys
import platform
import os
import re
import redo
import requests
import subprocess
import time
import ctypes
import shutil
import zipfile

from optparse import OptionParser

# Global variables

DEFAULT_SYMBOL_URL = "https://symbols.mozilla.org/upload/"
MAX_RETRIES = 5

# Utility classes

def read_output(*args):
    (stdout, _) = subprocess.Popen(args=args, stdout=subprocess.PIPE).communicate()
    return stdout.rstrip()

# Utility functions

if platform.system() == 'Windows':
    def normpath(path):
        '''
        Normalize a path using `GetFinalPathNameByHandleW` to get the
        path with all components in the case they exist in on-disk, so
        that making links to a case-sensitive server (hg.mozilla.org) works.

        This function also resolves any symlinks in the path.
        '''
        # Return the original path if something fails, which can happen for paths that
        # don't exist on this system (like paths from the CRT).
        result = path

        ctypes.windll.kernel32.SetErrorMode(ctypes.c_uint(1))
        if not isinstance(path, unicode):
            path = unicode(path, sys.getfilesystemencoding())
        handle = ctypes.windll.kernel32.CreateFileW(path,
                                                    # GENERIC_READ
                                                    0x80000000,
                                                    # FILE_SHARE_READ
                                                    1,
                                                    None,
                                                    # OPEN_EXISTING
                                                    3,
                                                    # FILE_FLAG_BACKUP_SEMANTICS
                                                    # This is necessary to open
                                                    # directory handles.
                                                    0x02000000,
                                                    None)
        if handle != -1:
            size = ctypes.windll.kernel32.GetFinalPathNameByHandleW(handle,
                                                                    None,
                                                                    0,
                                                                    0)
            buf = ctypes.create_unicode_buffer(size)
            if ctypes.windll.kernel32.GetFinalPathNameByHandleW(handle,
                                                                buf,
                                                                size,
                                                                0) > 0:
                # The return value of GetFinalPathNameByHandleW uses the
                # '\\?\' prefix.
                result = buf.value.encode(sys.getfilesystemencoding())[4:]
            ctypes.windll.kernel32.CloseHandle(handle)
        return result
else:
    # Just use the os.path version otherwise.
    normpath = os.path.normpath

def IsInDir(file, dir):
    # the lower() is to handle win32+vc8, where
    # the source filenames come out all lowercase,
    # but the srcdir can be mixed case
    return os.path.abspath(file).lower().startswith(os.path.abspath(dir).lower())

def GetPlatformSpecificDumper(**kwargs):
    """This function simply returns a instance of a subclass of Dumper
    that is appropriate for the current platform."""
    return {'WINNT': Dumper_Win32,
            'Linux': Dumper_Linux,
            'Darwin': Dumper_Mac}[platform.system()](**kwargs)

def SourceIndex(fileStream, outputPath, vcs_root):
    """Takes a list of files, writes info to a data block in a .stream file"""
    # Creates a .pdb.stream file in the mozilla\objdir to be used for source indexing
    # Create the srcsrv data block that indexes the pdb file
    result = True
    pdbStreamFile = open(outputPath, "w")
    pdbStreamFile.write('''SRCSRV: ini ------------------------------------------------\r\nVERSION=2\r\nINDEXVERSION=2\r\nVERCTRL=http\r\nSRCSRV: variables ------------------------------------------\r\nHGSERVER=''')
    pdbStreamFile.write(vcs_root)
    pdbStreamFile.write('''\r\nSRCSRVVERCTRL=http\r\nHTTP_EXTRACT_TARGET=%hgserver%/raw-file/%var3%/%var2%\r\nSRCSRVTRG=%http_extract_target%\r\nSRCSRV: source files ---------------------------------------\r\n''')
    pdbStreamFile.write(fileStream) # can't do string interpolation because the source server also uses this and so there are % in the above
    pdbStreamFile.write("SRCSRV: end ------------------------------------------------\r\n\n")
    pdbStreamFile.close()
    return result


class Dumper:
    """This class can dump symbols from a file with debug info, and
    store the output in a directory structure that is valid for use as
    a Breakpad symbol server.  Requires a path to a dump_syms binary--
    |dump_syms| and a directory to store symbols in--|symbol_path|.
    Optionally takes a list of processor architectures to process from
    each debug file--|archs|, the full path to the top source
    directory--|srcdir|, for generating relative source file names,
    and an option to copy debug info files alongside the dumped
    symbol files--|copy_debug|, mostly useful for creating a
    Microsoft Symbol Server from the resulting output.

    You don't want to use this directly if you intend to process files.
    Instead, call GetPlatformSpecificDumper to get an instance of a
    subclass."""
    srcdirRepoInfo = {}

    def __init__(self, dump_syms, symbol_path,
                 archs=None,
                 srcdirs=[],
                 copy_debug=False,
                 vcsinfo=False,
                 srcsrv=False,
                 generated_files=None,
                 s3_bucket=None,
                 file_mapping=None):
        # popen likes absolute paths, at least on windows
        self.dump_syms = os.path.abspath(dump_syms)
        self.symbol_path = symbol_path
        if archs is None:
            # makes the loop logic simpler
            self.archs = ['']
        else:
            self.archs = ['-a %s' % a for a in archs.split()]
        # Any paths that get compared to source file names need to go through normpath.
        self.srcdirs = [normpath(s) for s in srcdirs]
        self.copy_debug = copy_debug
        self.vcsinfo = vcsinfo
        self.srcsrv = srcsrv
        self.generated_files = generated_files or {}
        self.s3_bucket = s3_bucket
        self.file_mapping = file_mapping or {}

    # subclasses override this
    def ShouldProcess(self, file):
        return True

    def RunFileCommand(self, file):
        """Utility function, returns the output of file(1)"""
        # we use -L to read the targets of symlinks,
        # and -b to print just the content, not the filename
        print("RunFileCommand...")
        return read_output('file', '-Lb', file)

    # This is a no-op except on Win32
    def SourceServerIndexing(self, debug_file, guid, sourceFileStream, vcs_root):
        return ""

    # subclasses override this if they want to support this
    def CopyDebug(self, file, debug_file, guid, code_file, code_id):
        pass

    def Process(self, file_to_process, count_ctors=False):
        """Process the given file."""
        if self.ShouldProcess(os.path.abspath(file_to_process)):
            print("Dumper_Linux Dumper_Linux");
            self.ProcessFile(file_to_process, count_ctors=count_ctors)
        print("End of Dumper_Linux");

    def ProcessFile(self, file, dsymbundle=None, count_ctors=False):
        """Dump symbols from these files into a symbol file, stored
        in the proper directory structure in  |symbol_path|; processing is performed
        asynchronously, and Finish must be called to wait for it complete and cleanup.
        All files after the first are fallbacks in case the first file does not process
        successfully; if it does, no other files will be touched."""
        print("Beginning work for file: %s" % file, file=sys.stdout)

        # tries to get the vcs root from the .mozconfig first - if it's not set
        # the tinderbox vcs path will be assigned further down
        vcs_root = os.environ.get('MOZ_SOURCE_REPO')
        for arch_num, arch in enumerate(self.archs):
            self.ProcessFileWork(file, arch_num, arch, vcs_root, dsymbundle,
                                 count_ctors=count_ctors)

    def dump_syms_cmdline(self, file, arch, dsymbundle=None):
        '''
        Get the commandline used to invoke dump_syms.
        '''
        # The Mac dumper overrides this.
        return [self.dump_syms, file]

    def ProcessFileWork(self, file, arch_num, arch, vcs_root, dsymbundle=None,
                        count_ctors=False):
        ctors = 0
        t_start = time.time()
        print("Processing file: %s" % file, file=sys.stdout)

        sourceFileStream = ''
        code_id, code_file = None, None
        try:
            cmd = self.dump_syms_cmdline(file, arch, dsymbundle=dsymbundle)
            print(' '.join(cmd), file=sys.stdout)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=open(os.devnull, 'wb'))
            module_line = proc.stdout.next()
            print("......module_line line: %s" % module_line, file=sys.stdout)
            if module_line.startswith("MODULE"):
                # MODULE os cpu guid debug_file
                (guid, debug_file) = (module_line.split())[3:5]
                # strip off .pdb extensions, and append .sym
                sym_file = re.sub("\.pdb$", "", debug_file) + ".sym"
                # we do want forward slashes here
                rel_path = os.path.join(debug_file,
                                        guid,
                                        sym_file).replace("\\", "/")
                full_path = os.path.normpath(os.path.join(self.symbol_path,
                                                          rel_path))
                try:
                    os.makedirs(os.path.dirname(full_path))
                except OSError: # already exists
                    pass
                f = open(full_path, "w")
                f.write(module_line)
                # now process the rest of the output
                for line in proc.stdout:
                    if line.startswith("FILE"):
                        # FILE index filename
                        (x, index, filename) = line.rstrip().split(None, 2)
                        # We want original file paths for the source server.
                        sourcepath = filename
                        filename = normpath(filename)
                        if filename in self.file_mapping:
                            filename = self.file_mapping[filename]
                        if self.vcsinfo:
                            print("Unexpected error: Not support VCS.", file=sys.stderr)
                            break
                        # gather up files with hg for indexing
                        if filename.startswith("hg"):
                            (ver, checkout, source_file, revision) = filename.split(":", 3)
                            sourceFileStream += sourcepath + "*" + source_file + '*' + revision + "\r\n"
                        f.write("FILE %s %s\n" % (index, filename))
                    elif line.startswith("INFO CODE_ID "):
                        # INFO CODE_ID code_id code_file
                        # This gives some info we can use to
                        # store binaries in the symbol store.
                        bits = line.rstrip().split(None, 3)
                        if len(bits) == 4:
                            code_id, code_file = bits[2:]
                        f.write(line)
                    else:
                        if count_ctors and line.startswith("FUNC "):
                            # Static initializers, as created by clang and gcc
                            # have symbols that start with "_GLOBAL_sub"
                            if '_GLOBAL__sub_' in line:
                                ctors += 1
                            # MSVC creates `dynamic initializer for '...'`
                            # symbols.
                            elif "`dynamic initializer for '" in line:
                                ctors += 1

                        # pass through all other lines unchanged
                        f.write(line)
                f.close()
                proc.wait()
                # we output relative paths so callers can get a list of what
                # was generated
                print(rel_path)
                if self.srcsrv and vcs_root:
                    # add source server indexing to the pdb file
                    self.SourceServerIndexing(debug_file, guid, sourceFileStream, vcs_root)
                # only copy debug the first time if we have multiple architectures
                if self.copy_debug and arch_num == 0:
                    self.CopyDebug(file, debug_file, guid,
                                   code_file, code_id)
        except StopIteration:
            print("......module_line xxx", file=sys.stderr)
            pass
        except Exception as e:
            print("Unexpected error: %s" % str(e), file=sys.stderr)
            raise

        if dsymbundle:
            shutil.rmtree(dsymbundle)

        if count_ctors:
            import json

            perfherder_data = {
                "framework": {"name": "build_metrics"},
                "suites": [{
                    "name": "compiler_metrics",
                    "subtests": [{
                        "name": "num_static_constructors",
                        "value": ctors,
                        "alertChangeType": "absolute",
                        "alertThreshold": 3
                    }]}
                ]
            }
            perfherder_extra_options = os.environ.get('PERFHERDER_EXTRA_OPTIONS', '')
            for opt in perfherder_extra_options.split():
                for suite in perfherder_data['suites']:
                    if opt not in suite.get('extraOptions', []):
                        suite.setdefault('extraOptions', []).append(opt)

            if 'asan' not in perfherder_extra_options.lower():
                print('PERFHERDER_DATA: %s' % json.dumps(perfherder_data),
                    file=sys.stdout)

        elapsed = time.time() - t_start
        print('Finished processing %s in %.2fs' % (file, elapsed),
              file=sys.stdout)

# Platform-specific subclasses.  For the most part, these just have
# logic to determine what files to extract symbols from.

def locate_pdb(path):
    '''Given a path to a binary, attempt to locate the matching pdb file with simple heuristics:
    * Look for a pdb file with the same base name next to the binary
    * Look for a pdb file with the same base name in the cwd

    Returns the path to the pdb file if it exists, or None if it could not be located.
    '''
    path, ext = os.path.splitext(path)
    pdb = path + '.pdb'
    if os.path.isfile(pdb):
        return pdb
    # If there's no pdb next to the file, see if there's a pdb with the same root name
    # in the cwd. We build some binaries directly into dist/bin, but put the pdb files
    # in the relative objdir, which is the cwd when running this script.
    base = os.path.basename(pdb)
    pdb = os.path.join(os.getcwd(), base)
    if os.path.isfile(pdb):
        return pdb
    return None

class Dumper_Win32(Dumper):
    fixedFilenameCaseCache = {}

    def ShouldProcess(self, file):
        """This function will allow processing of exe or dll files that have pdb
        files with the same base name next to them."""
        if file.endswith(".exe") or file.endswith(".dll"):
            if locate_pdb(file) is not None:
                return True
        return False


    def CopyDebug(self, file, debug_file, guid, code_file, code_id):
        file = locate_pdb(file)
        def compress(path):
            compressed_file = path[:-1] + '_'
            # ignore makecab's output
            makecab = buildconfig.substs['MAKECAB']
            success = subprocess.call([makecab, "-D",
                                       "CompressionType=MSZIP",
                                       path, compressed_file],
                                      stdout=open(os.devnull, 'w'),
                                      stderr=subprocess.STDOUT)
            if success == 0 and os.path.exists(compressed_file):
                os.unlink(path)
                return True
            return False

        rel_path = os.path.join(debug_file,
                                guid,
                                debug_file).replace("\\", "/")
        full_path = os.path.normpath(os.path.join(self.symbol_path,
                                                  rel_path))
        shutil.copyfile(file, full_path)
        if compress(full_path):
            print(rel_path[:-1] + '_')
        else:
            print(rel_path)

        # Copy the binary file as well
        if code_file and code_id:
            full_code_path = os.path.join(os.path.dirname(file),
                                          code_file)
            if os.path.exists(full_code_path):
                rel_path = os.path.join(code_file,
                                        code_id,
                                        code_file).replace("\\", "/")
                full_path = os.path.normpath(os.path.join(self.symbol_path,
                                                          rel_path))
                try:
                    os.makedirs(os.path.dirname(full_path))
                except OSError as e:
                    if e.errno != errno.EEXIST:
                        raise
                shutil.copyfile(full_code_path, full_path)
                if compress(full_path):
                    print(rel_path[:-1] + '_')
                else:
                    print(rel_path)

    def SourceServerIndexing(self, debug_file, guid, sourceFileStream, vcs_root):
        # Creates a .pdb.stream file in the mozilla\objdir to be used for source indexing
        streamFilename = debug_file + ".stream"
        stream_output_path = os.path.abspath(streamFilename)
        # Call SourceIndex to create the .stream file
        result = SourceIndex(sourceFileStream, stream_output_path, vcs_root)
        if self.copy_debug:
            pdbstr_path = os.environ.get("PDBSTR_PATH")
            pdbstr = os.path.normpath(pdbstr_path)
            subprocess.call([pdbstr, "-w", "-p:" + os.path.basename(debug_file),
                             "-i:" + os.path.basename(streamFilename), "-s:srcsrv"],
                            cwd=os.path.dirname(stream_output_path))
            # clean up all the .stream files when done
            os.remove(stream_output_path)
        return result


class Dumper_Linux(Dumper):
    objcopy = os.environ['OBJCOPY'] if 'OBJCOPY' in os.environ else 'objcopy'
    def ShouldProcess(self, file):
        """This function will allow processing of files that are
        executable, or end with the .so extension, and additionally
        file(1) reports as being ELF files.  It expects to find the file
        command in PATH."""
        if file.endswith(".so") or os.access(file, os.X_OK):
            print("try to dump")
            return self.RunFileCommand(file).startswith("ELF")
        return False

    def CopyDebug(self, file, debug_file, guid, code_file, code_id):
        # We want to strip out the debug info, and add a
        # .gnu_debuglink section to the object, so the debugger can
        # actually load our debug info later.
        file_dbg = file + ".dbg"
        if subprocess.call([self.objcopy, '--only-keep-debug', file, file_dbg]) == 0 and \
           subprocess.call([self.objcopy, '--add-gnu-debuglink=%s' % file_dbg, file]) == 0:
            rel_path = os.path.join(debug_file,
                                    guid,
                                    debug_file + ".dbg")
            full_path = os.path.normpath(os.path.join(self.symbol_path,
                                                      rel_path))
            shutil.move(file_dbg, full_path)
            # gzip the shipped debug files
            os.system("gzip -4 -f %s" % full_path)
            print(rel_path + ".gz")
        else:
            if os.path.isfile(file_dbg):
                os.unlink(file_dbg)

class Dumper_Solaris(Dumper):
    def RunFileCommand(self, file):
        """Utility function, returns the output of file(1)"""
        try:
            output = os.popen("file " + file).read()
            return output.split('\t')[1];
        except:
            return ""

    def ShouldProcess(self, file):
        """This function will allow processing of files that are
        executable, or end with the .so extension, and additionally
        file(1) reports as being ELF files.  It expects to find the file
        command in PATH."""
        if file.endswith(".so") or os.access(file, os.X_OK):
            return self.RunFileCommand(file).startswith("ELF")
        return False

class Dumper_Mac(Dumper):
    def ShouldProcess(self, file):
        """This function will allow processing of files that are
        executable, or end with the .dylib extension, and additionally
        file(1) reports as being Mach-O files.  It expects to find the file
        command in PATH."""
        if file.endswith(".dylib") or os.access(file, os.X_OK):
            return self.RunFileCommand(file).startswith("Mach-O")
        return False

    def ProcessFile(self, file, count_ctors=False):
        print("Starting Mac pre-processing on file: %s" % file,
              file=sys.stdout)
        dsymbundle = self.GenerateDSYM(file)
        if dsymbundle:
            # kick off new jobs per-arch with our new list of files
            Dumper.ProcessFile(self, file, dsymbundle=dsymbundle,
                               count_ctors=count_ctors)

    def dump_syms_cmdline(self, file, arch, dsymbundle=None):
        '''
        Get the commandline used to invoke dump_syms.
        '''
        # dump_syms wants the path to the original binary and the .dSYM
        # in order to dump all the symbols.
        if dsymbundle:
            # This is the .dSYM bundle.
            return [self.dump_syms] + arch.split() + ['-g', dsymbundle, file]
        return Dumper.dump_syms_cmdline(self, file, arch)

    def GenerateDSYM(self, file):
        """dump_syms on Mac needs to be run on a dSYM bundle produced
        by dsymutil(1), so run dsymutil here and pass the bundle name
        down to the superclass method instead."""
        t_start = time.time()
        print("Running Mac pre-processing on file: %s" % (file,),
              file=sys.stdout)

        dsymbundle = file + ".dSYM"
        if os.path.exists(dsymbundle):
            shutil.rmtree(dsymbundle)
        dsymutil = buildconfig.substs['DSYMUTIL']
        # dsymutil takes --arch=foo instead of -a foo like everything else
        try:
            cmd = ([dsymutil] +
                   [a.replace('-a ', '--arch=') for a in self.archs if a] +
                   [file])
            print(' '.join(cmd), file=sys.stdout)
            subprocess.check_call(cmd, stdout=open(os.devnull, 'w'))
        except subprocess.CalledProcessError as e:
            print('Error running dsymutil: %s' % str(e), file=sys.stderr)
            raise

        if not os.path.exists(dsymbundle):
            # dsymutil won't produce a .dSYM for files without symbols
            print("No symbols found in file: %s" % (file,), file=sys.stderr)
            return False

        elapsed = time.time() - t_start
        print('Finished processing %s in %.2fs' % (file, elapsed),
              file=sys.stdout)
        return dsymbundle

    def CopyDebug(self, file, debug_file, guid, code_file, code_id):
        """ProcessFile has already produced a dSYM bundle, so we should just
        copy that to the destination directory. However, we'll package it
        into a .tar.bz2 because the debug symbols are pretty huge, and
        also because it's a bundle, so it's a directory. |file| here is the
        the original filename."""
        dsymbundle = file + '.dSYM'
        rel_path = os.path.join(debug_file,
                                guid,
                                os.path.basename(dsymbundle) + ".tar.bz2")
        full_path = os.path.abspath(os.path.join(self.symbol_path,
                                                  rel_path))
        success = subprocess.call(["tar", "cjf", full_path, os.path.basename(dsymbundle)],
                                  cwd=os.path.dirname(dsymbundle),
                                  stdout=open(os.devnull, 'w'), stderr=subprocess.STDOUT)
        if success == 0 and os.path.exists(full_path):
            print(rel_path)

def Upload_Symbol(zip_file):
    print("Uploading symbol file '{0}' to '{1}'".format(zip_file, DEFAULT_SYMBOL_URL), file=sys.stdout)
    zip_name = os.path.basename(zip_file)

    # Fetch the symbol server token from Taskcluster secrets
    secrets_url = "http://taskcluster/secrets/v1/secret/{}".format("project/firefoxreality/fr/symbols-token")
    res = requests.get(secrets_url)
    res.raise_for_status()
    secret = res.json()
    auth_token = secret["secret"]["token"]

    if len(auth_token) == 0:
        print("Failed to get the symbol token.", file=sys.stderr)

    for i, _ in enumerate(redo.retrier(attempts=MAX_RETRIES), start=1):
        print("Attempt %d of %d..." % (i, MAX_RETRIES))
        try:
            if zip_file.startswith("http"):
                zip_arg = {"data": {"url", zip_file}}
            else:
                zip_arg = {"files": {zip_name: open(zip_file, 'rb')}}
            r = requests.post(
                DEFAULT_SYMBOL_URL,
                headers={"Auth-Token": auth_token},
                allow_redirects=False,
                # Allow a longer read timeout because uploading by URL means the server
                # has to fetch the entire zip file, which can take a while. The load balancer
                # in front of symbols.mozilla.org has a 300 second timeout, so we'll use that.
                timeout=(10, 300),
                **zip_arg)
            # 500 is likely to be a transient failure.
            # Break out for success or other error codes.
            if r.status_code < 500:
                break
            print("Error: {0}".format(r), file=sys.stderr)
        except requests.exceptions.RequestException as e:
            print("Error: {0}".format(e), file=sys.stderr)
        print("Retrying...", file=sys.stdout)
    else:
        print("Maximun retries hit, giving up!", file=sys.stderr)
        return False

    if r.status_code >= 200 and r.status_code < 300:
        print("Uploaded successfully", file=sys.stdout)
        return True

    print("Upload symbols failed: {0}".format(r), file=sys.stderr)
    return False

# Entry point if called as a standalone program
def main():
    parser = OptionParser(usage="usage: %prog [options] <dump_syms binary> <symbol store path> <debug info files>")
    parser.add_option("-c", "--copy",
                      action="store_true", dest="copy_debug", default=False,
                      help="Copy debug info files into the same directory structure as symbol files")
    parser.add_option("-a", "--archs",
                      action="store", dest="archs",
                      help="Run dump_syms -a <arch> for each space separated cpu architecture in ARCHS (only on OS X)")
    parser.add_option("-s", "--srcdir",
                      action="append", dest="srcdir", default=[],
                      help="Use SRCDIR to determine relative paths to source files")
    parser.add_option("-v", "--vcs-info",
                      action="store_true", dest="vcsinfo",
                      help="Try to retrieve VCS info for each FILE listed in the output")
    parser.add_option("-i", "--source-index",
                      action="store_true", dest="srcsrv", default=False,
                      help="Add source index information to debug files, making them suitable for use in a source server.")
    parser.add_option("--install-manifest",
                      action="append", dest="install_manifests",
                      default=[],
                      help="""Use this install manifest to map filenames back
to canonical locations in the source repository. Specify
<install manifest filename>,<install destination> as a comma-separated pair.
""")
    parser.add_option("--count-ctors",
                      action="store_true", dest="count_ctors", default=False,
                      help="Count static initializers")
    (options, args) = parser.parse_args()

    #check to see if the pdbstr.exe exists
    if options.srcsrv:
        pdbstr = os.environ.get("PDBSTR_PATH")
        if not os.path.exists(pdbstr):
            print("Invalid path to pdbstr.exe - please set/check PDBSTR_PATH.\n", file=sys.stderr)
            sys.exit(1)

    if len(args) < 4:
        parser.error("not enough arguments")
        exit(1)

    # The path, args[2], should be ./app/build/intermediates/cmake/{DEVICE_NAME}/release/obj/armeabi-v7a/{LIB_NAME}.so
    lib_folder = args[2]
    lib_name = os.path.basename(lib_folder)
    device_name =  args[3]
    symbol_name = device_name + "-" + lib_name

    # Remove the existed symbol output folder
    symbol_folder = args[1] + "/" + lib_name
    output_folder = args[1] + "/zip/" + symbol_name
    if os.path.isdir(symbol_folder):
        shutil.rmtree(symbol_folder)
    if os.path.isdir(output_folder):
        shutil.rmtree(output_folder)

    # Check if the library exists.
    if not os.path.isfile(lib_folder):
        raise IOError(errno.ENOENT, 'The library file not found',
                      lib_folder)

    dumper = GetPlatformSpecificDumper(dump_syms=args[0],
                                       symbol_path=args[1],
                                       copy_debug=options.copy_debug,
                                       archs=options.archs,
                                       srcdirs=options.srcdir,
                                       vcsinfo=options.vcsinfo,
                                       srcsrv=options.srcsrv)
    dumper.Process(lib_folder, options.count_ctors)
    
    # Pack the symbol files to a zip file
    print("Start making a symbol zip file.", file=sys.stdout);
    
    # For symbols.mozilla.org, it needs us make a folder be called the lib's name.
    if not os.path.isdir(args[1] + "/zip"):
        os.mkdir(args[1] + "/zip", 0755)
    if not os.path.isdir(output_folder):
        os.mkdir(output_folder, 0755)

    try:
        shutil.copytree(symbol_folder, output_folder + "/" + lib_name)
    except OSError as e:
        raise IOError(errno.ENOENT, "Directory not copied", e)

    shutil.make_archive(output_folder , "zip", output_folder)
    print("End of packing file: %s" % output_folder + ".zip", file=sys.stdout)

    # Upload the symbol.zip to the symbol server.
    if not Upload_Symbol(output_folder + ".zip"):
        return 1

# run main if run directly
if __name__ == "__main__":
    main()
