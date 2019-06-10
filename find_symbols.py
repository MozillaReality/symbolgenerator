# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""
This script finds the libnative-lib.so and figures out which objcopy to use.
"""
import getopt;
import glob
import os
import subprocess
import sys

platforms = {
   'wavevr',
   'oculusvr3dof',
   'oculusvr',
   'googlevr',
   'noapi',
   'svr',
}

objcopyMap = {
   'arm' : 'tools/taskcluster/symbols/bin/arm-linux-androideabi-objcopy',
   'arm64' : 'tools/taskcluster/symbols/bin/aarch64-linux-android-objcopy',
   'x86' : 'tools/taskcluster/symbols/bin/i686-linux-android-objcopy',
}

def find_platform(path):
   global platforms
   values = path.split('/')
   for part in values:
      for platform in platforms:
         if part.find(platform) == 0:
            return part.lower()
   print 'Unable to find platform from path: "' + path + '"'
   sys.exit(1)

def find_objcopy(platform):
   global objcopyMap
   for arch, tool in objcopyMap.iteritems():
      if platform.endswith(arch):
         return tool
   print "Unknown platform architecture: " + platform
   sys.exit(1)

def main(name, argv):
   app = 'tools/taskcluster/symbols/symbolstore.py'
   dumpPath = 'tools/taskcluster/symbols/bin/dump_syms'
   symbolsPath = 'tools/taskcluster/symbols/crashreporter/crashreporter-symbols'
   try:
      opts, args = getopt.getopt(argv,"ha:d:s:")
   except getopt.GetoptError:
      print name + '-a <python script> -d <dump path> -s <symbols path>'
      sys.exit(2)
   for opt, arg in opts:
      if opt == '-h':
         print name + '-a <python script> -d <dump path> -s <symbols path>'
         sys.exit()
      elif opt in ("-a"):
         app = arg
      elif opt in ("-d"):
         dumpPath = arg
      elif opt in ("-s"):
         symbolsPath = arg

   for lib in glob.glob('./app/build/intermediates/cmake/*/release/obj/*/libnative-lib.so'):
      platform = find_platform(lib)
      objcopy = find_objcopy(platform)
      print 'platform: "' + platform + '" library: "' + lib + '" objcopy: "' + objcopy + '"'
      args = ['python', app, '-c', '-s', '.', dumpPath, symbolsPath, lib, platform]
      penv = dict(os.environ, OBJCOPY=objcopy)
      # print " ".join(args)
      print subprocess.check_output(args, env=penv)

if __name__ == "__main__":
   main(sys.argv[0], sys.argv[1:])
