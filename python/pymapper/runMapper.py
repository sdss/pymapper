""" Run mapping process
"""
from __future__ import division, absolute_import
import shutil
import subprocess

from functools import partial

import argparse
import time

from math import floor
import os
import glob
import sys

from twisted.internet import reactor

from sdss.utilities.astrodatetime import datetime

from .camera import Camera, sortDetections, IMGBASENAME, IMGEXTENSION, getScanParams, \
    pickleDetectionList, unpickleCentroids, multiprocessImage, pickleCentroids, sortDetections
# from .imgProcess import DetectedFiberList

from .motor import MotorController, MOTOR_CONFIG
from .pyCamera import GLOBALS

from .fiberAssign import SlitheadSolver, FocalSurfaceSolver

MJD = floor(datetime.now().mjd + 0.4) # MJD + 0.4 convention chosen by Holtz
BASEDIR = os.path.join(os.path.expanduser("~"), "scan", "%i"%MJD)
if not os.path.exists(BASEDIR):
    os.makedirs(BASEDIR)
# check for existing scan directories

tstart = None

# import cProfile, pstats, StringIO
# pr = cProfile.Profile()

"""
todo: add re-detect/re-solve options
"""

def copyConfig(scanDir):
    # copy the configuration files used in this scan
    # to the scan directory
    motorConfig = MOTOR_CONFIG.configFile
    cameraConfig = GLOBALS.configFile
    for configFile in [motorConfig, cameraConfig]:
        fileName = os.path.split(configFile)[-1]
        toFile = os.path.join(scanDir, fileName)
        print("copying %s to %s"%(configFile, toFile))
        shutil.copy(configFile, toFile)


def loadPlPlugMapM(plPlugMapPath):
    """Loads a plPlugMapM file to the DB.
    largely stolen from sdss_python_module/bin/plPlugMapM !!!
    """


    from sdss.internal.database.connections import LCODatabaseAdminLocalConnection
    from sdss.internal.database.apo.platedb.plPlugMapM import PlPlugMapMFile

    if not os.path.exists(plPlugMapPath):
        raise RuntimeError('file {0} cannot be found'.format(fScan))

    plFile = PlPlugMapMFile(plPlugMapPath, verbose=True)
    plFile.load(replace=True, active=True)

    return

def query_yes_no(question, default="yes"):
    """Ask a yes/no question via raw_input() and return their answer.

    "question" is a string that is presented to the user.
    "default" is the presumed answer if the user just hits <Enter>.
        It must be "yes" (the default), "no" or None (meaning
        an answer is required of the user).

    The "answer" return value is True for "yes" or False for "no".

    from: http://stackoverflow.com/questions/3041986/python-command-line-yes-no-input
    """
    valid = {"yes": True, "y": True, "ye": True,
             "no": False, "n": False}
    if default is None:
        prompt = " [y/n] "
    elif default == "yes":
        prompt = " [Y/n] "
    elif default == "no":
        prompt = " [y/N] "
    else:
        raise ValueError("invalid default answer: '%s'" % default)

    while True:
        sys.stdout.write(question + prompt)
        choice = raw_input().lower()
        if default is not None and choice == '':
            return valid[default]
        elif choice in valid:
            return valid[choice]
        else:
            sys.stdout.write("Please respond with 'yes' or 'no' "
                             "(or 'y' or 'n').\n")

def determineScanNumber(plateID, mjddir):
    # replace plPlugMapP-XXX with plPlugMapM-XXX
    # previousDirectory, filename = os.path.split(self.plPlugMap.filename)
    # determine any existing scans
    globStr = os.path.join(mjddir, "plPlugMapM-%i-*.par"%plateID)
    nExisting = glob.glob(globStr)
    # number this scan accordingly
    return len(nExisting) + 1

def pathPlugMapP(plateID):
    plateZfill = ("%i"%plateID).zfill(6)
    #replace 10s, 1s place with XX
    plateSubDir = plateZfill[:-2] + "XX"
    fileName = "plPlugMapP-%i.par"%plateID
    platelistdir = os.environ["PLATELIST_DIR"]
    return os.path.join(platelistdir, "plates", plateSubDir, plateZfill, fileName)


def getExistingImgs(scanDir):
    imgs = glob.glob(os.path.join(scanDir, "%s*.%s"%(IMGBASENAME, IMGEXTENSION)))
    return imgs


def _solvePlate(scanDir, plateID, cartID, fscanID, fscanMJD, plot=False, plugMapPath=None, dbLoad=True):
    # global pr
    # pr.disable()
    # s = StringIO.StringIO()
    # sortby = 'cumulative'
    # ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
    # ps.print_stats()
    # print(s.getvalue())
    global tstart
    centroidList = unpickleCentroids(scanDir)
    detectedFiberList = sortDetections(centroidList, plot=plot)
    pickleDetectionList(detectedFiberList, scanDir)
    shs = SlitheadSolver(detectedFiberList, centroidList)
    shs.matchDetections()
    print("missing fibers: ")
    for fiber in shs.missingFibers:
        print("fiber %i"%fiber)
    print("slit match rms: %.8f"%shs.rms)
    shs.plotSolution(scanDir)
    if plugMapPath is None:
        plugMapPath = pathPlugMapP(plateID)
    print("plugmap path", plugMapPath)
    assert os.path.exists(plugMapPath)
    fss = FocalSurfaceSolver(detectedFiberList, plugMapPath, scanDir, cartID, fscanID, fscanMJD)
    print("mapping done: took %.2f seconds"%(time.time()-tstart))
    if shs.missingFibers:
        subprocess.call("gnome-open %s/unplugged.png"%(scanDir), shell = True)
    # load the plPlugMap file as an active plugging in db
    if dbLoad:
        print("Loading plPlugMap in db, and making it active!!!")
        loadPlPlugMapM(fss.plPlugMap.filePath)

        print("copying plPlugMap file to /data/mapper/<MJD>")
        basePath, fileName = os.path.split(fss.plPlugMap.filePath)
        shutil.copy(fss.plPlugMap.filePath, os.path.join("/data/mapper/%i"%MJD, fileName))

        #print("closing screen log")
        #subprocess.call("exit")

        print("copying config files to scan dir")
        copyConfig(scanDir)
        # compress all images in the scan directory
        print("compressing fits files")
        subprocess.call("fpack -D *.fits", cwd=scanDir, shell=True)
        print("copying all map data to /data/rawmapper")
        rawmapperDir = "/data/rawmapper/%i/plate%i/fscan%i"%(fscanMJD, plateID, fscanID)
        print("creating %s"%rawmapperDir)
        os.makedirs(rawmapperDir)
        print("cp %s/* %s"%(scanDir, rawmapperDir))
        print("copying all files to /data/rawmapper in background")
        subprocess.Popen("cp %s/* %s"%(scanDir, rawmapperDir), shell=True)
        print("Map Finished")

        # We print a the detections and missing fibres again so that they are the
        # last thing the pluggers see.

        detected_str = '# fibres detected: {0}'.format(len(shs.detectedFiberList))
        missing_str = '# fibres missing: {0}'.format(len(shs.missingFibers))

        if len(shs.missingFibers) == 0:
            list_missing_str = None
        else:
            list_missing_str = 'Missing fibres: {0}'.format(', '.join(map(str, shs.missingFibers)))

        max_length = max(len(detected_str), len(missing_str),
                         0 if list_missing_str is None else len(list_missing_str))
        hash_padding = '#' * max_length

        print('')
        print(hash_padding)
        print(detected_str)
        print(missing_str)
        if list_missing_str:
            print(list_missing_str)
        print(hash_padding)
        print('')

        # Kills itself for good measure.
        subprocess.call("killall -9 python", shell=True)


def reprocessImgs(args):
    # global pr
    # pr.enable()
    # get all relative information
    # from existing log file
    if args.scanDir is None:
        raise RuntimeError("Must specify --scanDir with --reprocessImgs")
    baseDir = os.path.abspath(args.rootDir)
    scanDir = os.path.join(baseDir, args.scanDir)
    if not os.path.exists(scanDir):
        raise RuntimeError("Scan directory doesn't exist: %s"%scanDir)
    # verify that images exist
    imgs = getExistingImgs(scanDir)
    if not imgs:
        raise RuntimeError("Scan directory doesn't contain existing imgs, cannot --reprocess!")
    print("reprocessing images in %s"%scanDir)
    scanParams = getScanParams(os.path.join(scanDir, "scanParams.par"))
    startPos = scanParams["start"]
    endPos = scanParams["end"]
    scanSpeed = scanParams["speed"]
    # create directory to hold camera images
    # note all previous images will be removed if image dir is not empty
    camera = Camera(scanDir, startPos, endPos, scanSpeed)
    solvePlate = partial(_solvePlate, scanDir=scanDir, plateID=args.plateID, plot=args.plotDetections)
    camera.doneProcessingCallback(solvePlate)
    camera.reprocessImages()


def runScan(args):
    """Move motor, take images, etc
    """
    if not args.plateID:
        plateID = int(raw_input("plateID: "))
    else:
        plateID = int(args.plateID)
    if not args.cartID:
        cartID = int(raw_input("cartID: "))
    else:
        cartID = int(args.cartID)
    plateDir = os.path.join(BASEDIR, "plate%i"%plateID)
    if not os.path.exists(plateDir):
        os.makedirs(plateDir)
    # now determine how many scans already exist for this plate
    for fscanID in range(1,10000):
        scanDir = os.path.join(plateDir, "fscan%i"%fscanID)
        if not os.path.exists(scanDir):
            break
    os.makedirs(scanDir)
    # if the MJD directory in /data/mapper doesn't yet exist, make it now
    # plPlugMaps will also be copied there (for utah to grab)
    if not os.path.exists("/data/mapper/%i"%MJD):
        os.makedirs("/data/mapper/%i"%MJD)
    print("scanDir: %s"%scanDir)
    print("plate ID: %i"%plateID)
    print("motor start pos (mm): %.2f"%MOTOR_CONFIG.startPos)
    print("motor end pos (mm): %.2f"%MOTOR_CONFIG.endPos)
    print("motor scan speed (mm/sec): %.2f"%MOTOR_CONFIG.speed)


    # create directory to hold camera images
    # note all previous images will be removed if image dir is not empty
    camera = Camera(scanDir)

    # construct motor, nothing happens until connect is called
    motorController = MotorController()

    # set up callback chains for mapping process

    moveMotor = partial(motorController.scan, callFunc=camera.stopAcquisition)
    beginImgAcquisition = partial(camera.beginAcquisition, callFunc=moveMotor)
    solvePlate = partial(_solvePlate, scanDir, plateID, cartID, fscanID, MJD, plot=args.plotDetections)
    camera.doneProcessingCallback(solvePlate)
    motorController.addReadyCallback(beginImgAcquisition)
    motorController.connect()
    reactor.run()


def resolve(scanDir=None, plateID=None, cartID=None, fscanID=None, fscanMJD=None):
    """Rerun a map from an existing detectionList, finds expected input files (images, centriods, detections, plPlugMapP)
    from the working directory, writes output to the same directory (images, plPlugMapM)
    """
    global tstart
    tstart = time.time()
    if scanDir is None:
        scanDir = os.getcwd()
    if plateID is None:
        plPlugMapFile = glob.glob(os.path.join(scanDir, "plPlugMapP-*.par"))
        plPlugMapFile = plPlugMapFile[0]
        # get the plateID from the plPlug filename
        plateID = int(os.path.split(plPlugMapFile)[-1].strip("plPlugMapP-").strip(".par"))
        if not plPlugMapFile:
            raise RuntimeError("No plPlugMapP file found")
        if not len(plPlugMapFile)==1:
            raise RuntimeError("Found multiple plPlugMap files!")
    else:
        plPlugMapFile = pathPlugMapP(plateID)
    if cartID is None:
        cartID = 99
    if fscanID is None:
        fscanID = 99
    if fscanMJD is None:
        fscanMJD = 9999

    _solvePlate(scanDir, plateID, cartID, fscanID, fscanMJD, plot=False, plugMapPath=plPlugMapFile, dbLoad=False)


def main(argv=None):
    global baseDir
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Run the plate mapper."
        )
    parser.add_argument("--plateID", type=int, required=False, help="Plate ID")
    parser.add_argument("--cartID", type=int, required=False, help="Cart ID")
    parser.add_argument("--plotDetections", action="store_true", default=False, help="if present create png plots with circled dectections, takes much longer." )
    args = parser.parse_args()
    global tstart
    tstart = time.time()
    runScan(args)




if __name__ == "__main__":
    main()
