"""
This implements the WOFF specification dated September 16, 2009.

The main object is the WOFFFont. It is a subclass for the FontTools
TTFont object, so it has very similar functionality. The WOFFReader
and WOFFWriter are also available for use outside of this module.
Those objects are much faster than WOFFFont, but they require much
more care.
"""

import zlib
import struct
import sstruct
import numpy
from cStringIO import StringIO
from xml.etree import ElementTree
from fontTools.ttLib import TTFont, debugmsg, sortedTagList
from fontTools.ttLib.sfnt import getSearchRange, calcChecksum, SFNTDirectoryEntry, \
    sfntDirectoryFormat, sfntDirectorySize, sfntDirectoryEntryFormat, sfntDirectoryEntrySize


# -----------
# Main Object
# -----------

class WOFFFont(TTFont):

    """
    This object represents a WOFF file. It is a subclass of
    the FontTools TTFont object, so the same API applies.
    For information about the arguments in __init__,
    refer to the TTFont documentation.

    This object has two special attributes: metadata and privateData.
    The metadata attribute returns an ElementTree Element object
    representing the metadata stored in the font. To set new metadata
    in the font, you must use this object. The privateData attribute
    returns the private data stored in the font. To set private data,
    set a string to font.privateData.
    """

    def __init__(self, file=None, flavor="\000\001\000\000",
        checkChecksums=0, verbose=False, recalcBBoxes=True,
        allowVID=False, ignoreDecompileErrors=False):
        # can't use the TTFont __init__ because it goes directly to the SFNTReader.
        # see that method for details about all of this.
        self.verbose = verbose
        self.recalcBBoxes = recalcBBoxes
        self.tables = {}
        self.reader = None

        self.last_vid = 0xFFFE
        self.reverseVIDDict = {}
        self.VIDDict = {}
        self.allowVID = allowVID

        self.flavor = flavor
        self.majorVersion = 0
        self.minorVersion = 0
        self._metadata = None
        self._tableOrder = None

        if file is not None:
            if not hasattr(file, "read"):
                file = open(file, "rb")
            self.reader = WOFFReader(file, checkChecksums=checkChecksums)
            self.flavor = self.reader.flavor
            self.majorVersion = self.reader.majorVersion
            self.minorVersion = self.reader.minorVersion
            self._tableOrder = self.reader.keys()
        else:
            self._metadata = ElementTree.Element("metadata", version="1.0")
            self.privateData = None

    def __getattr__(self, attr):
        if attr not in ("privateData", "metadata"):
            raise AttributeError(attr)
        # metadata
        if attr == "metadata":
            if self._metadata is not None:
                return self._metadata
            if self.reader is not None:
                text = self.reader.metadata
                if text:
                    metadata = ElementTree.fromstring(text)
                else:
                    metadata = ElementTree.Element("metadata", version="1.0")
                self._metadata = metadata
                return self._metadata
            return None
        # private data
        elif attr == "privateData":
            if not hasattr(self, "privateData"):
                privateData = None
                if self.reader is not None:
                    privateData = self.reader.privateData
                self.privateData = privateData
            return self.privateData
        # fallback to None
        return None

    def keys(self):
        """
        Return a list of all tables in the font. If a table order
        has been set manually or as the result of opening an existing
        WOFF file, the set table order will be in the list first.
        Tables not defined in an existing order will be sorted following
        the suggested ordering in the OTF/OFF specification.

        The first table listed in all cases is the GlyphOrder pseudo table.
        """
        tags = set(self.tables.keys())
        if self.reader is not None:
            tags = tags | set(self.reader.keys())
        tags = list(tags)
        if "GlyphOrder" in tags:
            tags.remove("GlyphOrder")
        return ["GlyphOrder"] + sortedTagList(tags, self._tableOrder)

    def setTableOrder(self, order):
        """
        Set the order in which tables should be written
        into the font. This is required if a DSIG table
        is in the font.
        """
        self._tableOrder = order

    def save(self, file, compressionLevel=9, recompressTables=False, reorderTables=True, recalculateHeadChecksum=True):
        """
        Save a WOFF into file a file object specifified by the
        file argument.. Optionally, file can be a path and a
        new file will be created at that location.

        compressionLevel is the compression level to be
        used with zlib. This must be an int between 1 and 9.
        The default is 9, the highest compression, but slowest
        compression time.

        Set recompressTables to True if you want any already
        compressed tables to be decompressed and then recompressed
        using the level specified by compressionLevel.

        If you want the tables in the WOFF reordered following
        the suggested optimal table orderings described in the
        OTF/OFF sepecification, set reorderTables to True.
        Tables cannot be reordered if a DSIG table is in the font.

        If you change any of the SFNT data or reorder the tables,
        the head table checkSumAdjustment must be recalculated.
        If you are not changing any of the SFNT data, you can set
        recalculateHeadChecksum to False to prevent the recalculation.
        This must be set to False if the font contains a DSIG table.
        """
        # if DSIG is to be written, the table order
        # must be completely specified. otherwise the
        # DSIG may not be valid after decoding the WOFF.
        tags = self.keys()
        if "GlyphOrder" in tags:
            tags.remove("GlyphOrder")
        if "DSIG" in tags:
            if self._tableOrder is None or (set(self._tableOrder) != set(tags)):
                raise WOFFLibError("A complete table order must be supplied when saving a font with a 'DSIG' table.")
            elif reorderTables:
                raise WOFFLibError("Tables can not be reordered when a 'DSIG' table is in the font. Set reorderTables to False.")
            elif recalculateHeadChecksum:
                raise WOFFLibError("The 'head' table checkSumAdjustment can not be recalculated when a 'DSIG' table is in the font.")
        # sort the tags if necessary
        if reorderTables:
            tags = sortedTagList(tags)
        # open a file if necessary
        closeStream = False
        if not hasattr(file, "write"):
            closeStream = True
            file = open(file, "wb")
        # write the table data
        if "GlyphOrder" in tags:
            tags.remove("GlyphOrder")
        numTables = len(tags)
        writer = WOFFWriter(file, numTables, flavor=self.flavor,
            majorVersion=self.majorVersion, minorVersion=self.minorVersion,
            compressionLevel=compressionLevel, recalculateHeadChecksum=recalculateHeadChecksum,
            verbose=self.verbose)
        for tag in tags:
            origData = None
            origLength = None
            origChecksum = None
            compLength = None
            # table is loaded
            if self.isLoaded(tag):
                origData = self.getTableData(tag)
            # table is in reader
            elif self.reader is not None:
                if recompressTables:
                    origData = self.getTableData(tag)
                else:
                    if self.verbose:
                        debugmsg("Reading '%s' table from disk" % tag)
                    origData, origLength, origChecksum, compLength = self.reader.getCompressedTableData(tag)
            # add to writer
            writer.setTable(tag, origData, origLength=origLength, origChecksum=origChecksum, compLength=compLength)
        # write the metadata
        metadata = None
        metaOrigLength = None
        metaLength = None
        if hasattr(self, "metadata"):
            tree = ElementTree.ElementTree(self.metadata)
            f = StringIO()
            tree.write(f, encoding="utf-8")
            metadata = f.getvalue()
            del f
        elif self.reader is not None:
            if recompressTables:
                metadata = self.reader.metadata
            else:
                metadata, metaOrigLength, metaLength = self.reader.getCompressedMetadata()
        if metadata:
            writer.setMetadata(metadata, metaOrigLength=metaOrigLength, metaLength=metaLength)
        # write the private data
        privData = self.privateData
        if privData:
            writer.setPrivateData(privData)
        # close the writer
        writer.close()
        # close the file
        if closeStream:
            file.close()

    def saveXML(self):
        raise NotImplementedError

    def importXML(self):
        raise NotImplementedError


# ------
# Reader
# ------

woffHeaderFormat = """
    > # big endian
    signature:      4s
    flavor:         4s
    length:         l
    numTables:      H
    reserved:       H
    totalSFNTSize:  l
    majorVersion:   H
    minorVersion:   H
    metaOffset:     l
    metaLength:     l
    metaOrigLength: l
    privOffset:     l
    privLength:     l
"""
woffHeaderSize = sstruct.calcsize(woffHeaderFormat)

class WOFFReader(object):

    def __init__(self, file, checkChecksums=1):
        self.file = file
        self.checkChecksums = checkChecksums
        # unpack the header
        self.file.seek(0)
        bytes = self.file.read(woffHeaderSize)
        sstruct.unpack(woffHeaderFormat, bytes, self)
        # unpack the directory
        self.tables = {}
        for i in range(self.numTables):
            entry = WOFFDirectoryEntry()
            entry.fromFile(self.file)
            self.tables[entry.tag] = entry

    def close(self):
        self.file.close()

    def __contains__(self, tag):
        return tag in self.tables

    has_key = __contains__

    def keys(self):
        """
        This returns a list of all tables in the WOFF
        sorted in ascending order based on the offset
        of each table.
        """
        sorter = []
        for tag, entry in self.tables.items():
            sorter.append((entry.offset, tag))
        order = [tag for offset, tag in sorted(sorter)]
        return order

    def __getitem__(self, tag):
        entry = self.tables[tag]
        self.file.seek(entry.offset)
        data = self.file.read(entry.compLength)
        # decompress if necessary
        if entry.compLength < entry.origLength:
            data = zlib.decompress(data)
        else:
            data = data[:entry.origLength]
        # compare the checksums
        if self.checkChecksums:
            checksum = calcTableChecksum(tag, data)
            if self.checkChecksums > 1:
                assert checksum == entry.origChecksum, "bad checksum for '%s' table" % tag
            elif checksum != entry.origChecksum:
                print "bad checksum for '%s' table" % tag
            print
        return data

    def getCompressedTableData(self, tag):
        entry = self.tables[tag]
        self.file.seek(entry.offset)
        data = self.file.read(entry.compLength)
        return data, entry.origLength, entry.origChecksum, entry.compLength

    def getCompressedMetadata(self):
        self.file.seek(self.metaOffset)
        data = self.file.read(self.metaLength)
        return data, self.metaOrigLength, self.metaLength

    def __getattr__(self, attr):
        if attr not in ("privateData", "metadata"):
            raise AttributeError(attr)
        if attr == "privateData":
            self.file.seek(self.privOffset)
            return self.file.read(self.privLength)
        if attr == "metadata":
            self.file.seek(self.metaOffset)
            data = self.file.read(self.metaLength)
            if self.metaLength:
                data = zlib.decompress(data)
                assert len(data) == self.metaOrigLength
            return data

    def __delitem__(self, tag):
        del self.tables[tag]


# ------
# Writer
# ------

class WOFFWriter(object):

    def __init__(self, file, numTables, flavor="\000\001\000\000",
            majorVersion=0, minorVersion=0, compressionLevel=9,
            recalculateHeadChecksum=True,
            verbose=False):
        self.signature = "wOFF"
        self.flavor = flavor
        self.length = woffHeaderSize + (numTables * woffDirectoryEntrySize)
        self.totalSFNTSize = sfntDirectorySize + (numTables * sfntDirectoryEntrySize)
        self.numTables = numTables
        self.majorVersion = majorVersion
        self.minorVersion = minorVersion
        self.metaOffset = 0
        self.metaOrigLength = 0
        self.metaLength = 0
        self.privOffset = 0
        self.privLength = 0
        self.reserved = 0

        self.file = file
        self.compressionLevel = compressionLevel
        self.recalculateHeadChecksum = recalculateHeadChecksum
        self.verbose = verbose

        # the data is held to facilitate the
        # head checkSumAdjustment calculation.
        self.tables = {}
        self.metadata = None
        self.privateData = None
        self.tableDataEnd = 0
        self.metadataEnd = 0

    def _tableOrder(self):
        return [entry.tag for index, entry, data in sorted(self.tables.values())]

    def setTable(self, tag, data, origLength=None, origChecksum=None, compLength=None):
        # don't compress the head if the checkSumAdjustment needs to be recalculated
        # the compression will be handled later.
        if self.recalculateHeadChecksum and tag == "head":
            # decompress
            if compLength is not None:
                data = zlib.decompress(data)
            entry = self._prepTable(tag, data, origLength=len(data), entryOnly=True)
        # compress
        else:
            entry, data = self._prepTable(tag, data=data, origLength=origLength, origChecksum=origChecksum, compLength=compLength)
        # store
        self.tables[tag] = (len(self.tables), entry, data)

    def setMetadata(self, data, metaOrigLength=None, metaLength=None):
        if not data:
            return
        if metaLength is None:
            if self.verbose:
                debugmsg("compressing metadata")
            metaOrigLength = len(data)
            data = zlib.compress(data, self.compressionLevel)
            metaLength = len(data)
        # set the header values
        self.metaOrigLength = metaOrigLength
        self.metaLength = metaLength
        # store
        self.metadata = data

    def setPrivateData(self, data):
        if not data:
            return
        privLength = len(data)
        # set the header value
        self.privLength = privLength
        # store
        self.privateData = data

    def close(self):
        if self.numTables != len(self.tables):
            raise WOFFLibError("wrong number of tables; expected %d, found %d" % (self.numTables, len(self.tables)))
        # first, handle the checkSumAdjustment
        if self.recalculateHeadChecksum and "head" in self.tables:
            self._handleHeadChecksum()
        # write the header
        header = sstruct.pack(woffHeaderFormat, self)
        self.file.seek(0)
        self.file.write(header)
        # update the directory offsets
        offset = woffHeaderSize + (woffDirectoryEntrySize * self.numTables)
        order = self._tableOrder()
        for tag in order:
            index, entry, data = self.tables[tag]
            entry.offset = offset
            offset += calc4BytePaddedLength(entry.compLength) # ensure byte alignment
        # write the directory
        self._writeTableDirectory()
        # write the table data
        self._writeTableData()
        # write the metadata
        self._writeMetadata()
        # write the private data
        self._writePrivateData()
        # write the header
        self._writeHeader()
        # go to the beginning of the file
        self.file.seek(0)

    # header support

    def _writeHeader(self):
        header = sstruct.pack(woffHeaderFormat, self)
        self.file.seek(0)
        self.file.write(header)

    # sfnt support

    def _prepTable(self, tag, data, origLength=None, origChecksum=None, compLength=None, entryOnly=False):
        # skip data prep
        if entryOnly:
            origLength = origLength
            origChecksum = calcTableChecksum(tag, data)
            compLength = 0
        # prep the data
        else:
            # compress
            if compLength is None:
                origData = data
                origLength = len(origData)
                origChecksum = calcTableChecksum(tag, data)
                if self.verbose:
                    debugmsg("compressing '%s' table" % tag)
                compData = zlib.compress(origData, self.compressionLevel)
                compLength = len(compData)
                if origLength <= compLength:
                    data = origData
                    compLength = origLength
                else:
                    data = compData
        # make the directory entry
        entry = WOFFDirectoryEntry()
        entry.tag = tag
        entry.offset = 0
        entry.origLength = origLength
        entry.origChecksum = origChecksum
        entry.compLength = compLength
        # return
        if entryOnly:
            return entry
        return entry, data

    def _handleHeadChecksum(self):
        if self.verbose:
            debugmsg("updating head checkSumAdjustment")
        # build the sfnt header
        searchRange, entrySelector, rangeShift = getSearchRange(self.numTables)
        sfntDirectoryData = dict(
            sfntVersion=self.flavor,
            numTables=self.numTables,
            searchRange=searchRange,
            entrySelector=entrySelector,
            rangeShift=rangeShift
        )
        # build the sfnt directory
        directory = sstruct.pack(sfntDirectoryFormat, sfntDirectoryData)
        sfntEntries = {}
        offset = sfntDirectorySize + (sfntDirectoryEntrySize * self.numTables)
        for (index, entry, data) in sorted(self.tables.values()):
            sfntEntry = SFNTDirectoryEntry()
            sfntEntry.tag = entry.tag
            sfntEntry.checkSum = entry.origChecksum
            sfntEntry.offset = offset
            sfntEntry.length = entry.origLength
            sfntEntries[entry.tag] = sfntEntry
            offset += calc4BytePaddedLength(entry.origLength)
        for tag, sfntEntry in sorted(sfntEntries.items()):
            directory += sfntEntry.toString()
        # calculate the checkSumAdjustment
        ## adapted from SFNTWriter.calcMasterChecksum
        tags = sfntEntries.keys()
        checksums = numpy.zeros(len(tags) + 1, numpy.int32)
        for index, tag in enumerate(tags):
            checksums[index] = sfntEntries[tag].checkSum
        directoryEnd = sfntDirectorySize + (len(tags) * sfntDirectoryEntrySize)
        assert directoryEnd == len(directory)
        checksums[-1] = calcChecksum(directory)
        checksum = numpy.add.reduce(checksums)
        checkSumAdjustment = numpy.array(0xb1b0afbaL - 0x100000000L, numpy.int32) - checksum
        # set the value in the head table
        index, entry, data = self.tables["head"]
        data = data[:8] + struct.pack(">l", checkSumAdjustment) + data[12:]
        # compress the data
        newEntry, data = self._prepTable("head", data)
        # update the entry data
        assert entry.origChecksum == newEntry.origChecksum
        entry.origLength = newEntry.origLength
        entry.compLength = newEntry.compLength
        # store
        self.tables["head"] = (index, entry, data)

    def _writeTableDirectory(self):
        if self.verbose:
            debugmsg("writing table directory")
        self.file.seek(woffHeaderSize)
        for tag, (index, entry, data) in sorted(self.tables.items()):
            entry = sstruct.pack(woffDirectoryEntryFormat, entry)
            self.file.write(entry)

    def _writeTableData(self):
        d = woffHeaderSize + (woffDirectoryEntrySize * self.numTables)
        offset = woffHeaderSize + (woffDirectoryEntrySize * self.numTables)
        self.file.seek(offset)
        for tag in self._tableOrder():
            if self.verbose:
                debugmsg("writing '%s' table" % tag)
            index, entry, data = self.tables[tag]
            data += "\0" * (calc4BytePaddedLength(entry.compLength) - entry.compLength ) # ensure byte alignment
            self.file.write(data)
            self.length += calc4BytePaddedLength(entry.compLength) # ensure byte alignment
            self.totalSFNTSize += calc4BytePaddedLength(entry.origLength) # ensure byte alignment
        # store the end for use by metadata or private data
        self.tableDataEnd = self.length

    # metadata support

    def _writeMetadata(self):
        if self.metadata is None:
            return
        if self.verbose:
            debugmsg("writing metadata")
        self.length += self.metaLength
        self.metaOffset = self.tableDataEnd
        self.file.seek(self.metaOffset)
        self.file.write(self.metadata)
        # store the end for use by private data
        self.metadataEnd = self.metaOffset + self.metaLength
        # if private data exists, pad to a four byte boundary
        if self.privateData is not None:
            padding = calc4BytePaddedLength(self.metaLength) - self.metaLength
            self.metadataEnd += padding
            self.length += padding
            padding = "\0" * padding
            if padding:
                self.file.write(padding)

    # private data support

    def _writePrivateData(self):
        if self.privateData is None:
            return
        if self.verbose:
            debugmsg("writing private data")
        if self.metadata is not None:
            self.privOffset = self.metadataEnd
        else:
            self.privOffset = self.tableDataEnd
        self.length += self.privLength
        self.file.seek(self.privOffset)
        self.file.write(self.privateData)


# ---------
# Directory
# ---------

woffDirectoryEntryFormat = """
    > # big endian
    tag:            4s
    offset:         l
    compLength:     l
    origLength:     l
    origChecksum:   l
"""
woffDirectoryEntrySize = sstruct.calcsize(woffDirectoryEntryFormat)

class WOFFDirectoryEntry(object):

    def fromFile(self, file):
        sstruct.unpack(woffDirectoryEntryFormat, file.read(woffDirectoryEntrySize), self)

    def fromString(self, str):
        sstruct.unpack(woffDirectoryEntryFormat, str, self)

    def toString(self):
        return sstruct.pack(woffDirectoryEntryFormat, self)

    def __repr__(self):
        if hasattr(self, "tag"):
            return "<WOFFDirectoryEntry '%s' at %x>" % (self.tag, id(self))
        else:
            return "<WOFFDirectoryEntry at %x>" % id(self)


# -------
# Helpers
# -------

class WOFFLibError(Exception): pass

def calc4BytePaddedLength(length):
    return (length + 3) & ~3

def calcTableChecksum(tag, data):
    if tag == "head":
        checksum = calcChecksum(data[:8] + '\0\0\0\0' + data[12:])
    else:
        checksum = calcChecksum(data)
    return checksum


# ----------------
# SFNT Conformance
# ----------------

def checkSFNTConformance(data):
    errors = []
    # unpack the header
    headerData = data[:sfntDirectorySize]
    header = sstruct.unpack(sfntDirectoryFormat, headerData)
    # unpack the table directory
    numTables = header["numTables"]
    directoryData = data[sfntDirectorySize : sfntDirectorySize + (sfntDirectoryEntrySize * numTables)]
    tableDirectory = []
    for index in range(numTables):
        entry = sstruct.unpack(sfntDirectoryEntryFormat, directoryData[:sfntDirectoryEntrySize])
        tableDirectory.append(entry)
        directoryData = directoryData[sfntDirectoryEntrySize:]
    # sanity testing
    errors += _testOffsetValidity(len(data), tableDirectory)
    errors += _testLengthValidity(len(data), tableDirectory)
    # if one or more errors have already been found, something
    # is very wrong and this should come to a screeching halt.
    if errors:
        return errors
    # lod the table data
    for entry in tableDirectory:
        offset = entry["offset"]
        length = entry["length"]
        entry["data"] = data[offset:offset+length]
    # test for padding
    errors += _testPadding(tableDirectory)
    # test the final table padding
    errors += _testFinalTablePadding(len(data), numTables, tableDirectory[-1]["tag"])
    # test for gaps
    errors += _testGaps(tableDirectory)
    # test for a gap at the end of the file
    errors += _testGapAfterFinalTable(len(data), tableDirectory)
    # validate checksums
    errors += _testCheckSums(tableDirectory)
    # done.
    return errors

def _testOffsetValidity(dataLength, tableDirectory):
    """
    >>> test = [
    ...     dict(tag="test", offset=44)
    ... ]
    >>> bool(_testOffsetValidity(45, test))
    False
    >>> test = [
    ...     dict(tag="test", offset=1)
    ... ]
    >>> bool(_testOffsetValidity(45, test))
    True
    >>> test = [
    ...     dict(tag="test", offset=46)
    ... ]
    >>> bool(_testOffsetValidity(45, test))
    True
    """
    errors = []
    numTables = len(tableDirectory)
    minOffset = sfntDirectorySize + (sfntDirectoryEntrySize * numTables)
    for entry in tableDirectory:
        offset = entry["offset"]
        tag = entry["tag"]
        if offset < minOffset:
            errors.append("The offset to the %s table is not valid." % tag)
        if offset > dataLength:
            errors.append("The offset to the %s table is not valid." % tag)
    return errors

def _testLengthValidity(dataLength, tableDirectory):
    """
    >>> test = [
    ...     dict(tag="test", offset=44, length=1)
    ... ]
    >>> bool(_testLengthValidity(45, test))
    False
    >>> test = [
    ...     dict(tag="test", offset=44, length=2)
    ... ]
    >>> bool(_testLengthValidity(45, test))
    True
    """
    errors = []
    for entry in tableDirectory:
        offset = entry["offset"]
        length = entry["length"]
        tag = entry["tag"]
        end = offset + length
        if end > dataLength:
            errors.append("The length of the %s table is not valid." % tag)
    return errors

def _testPadding(tableDirectory):
    """
    >>> test = [
    ...     dict(tag="test", offset=1)
    ... ]
    >>> bool(_testPadding(test))
    True
    >>> test = [
    ...     dict(tag="test", offset=2)
    ... ]
    >>> bool(_testPadding(test))
    True
    >>> test = [
    ...     dict(tag="test", offset=3)
    ... ]
    >>> bool(_testPadding(test))
    True
    >>> test = [
    ...     dict(tag="test", offset=4)
    ... ]
    >>> bool(_testPadding(test))
    False
    """
    errors = []
    prevTable = None
    for entry in tableDirectory:
        offset = entry["offset"]
        if offset % 4:
            errors.append("The %s table is not properly padded." % prevTable)
        prevTable = entry["tag"]
    return errors

def _testFinalTablePadding(dataLength, numTables, finalTableTag):
    """
    >>> bool(_testFinalTablePadding(
    ...     sfntDirectorySize + sfntDirectoryEntrySize + 1,
    ...     1,
    ...     "test"
    ... ))
    True
    >>> bool(_testFinalTablePadding(
    ...     sfntDirectorySize + sfntDirectoryEntrySize + 2,
    ...     1,
    ...     "test"
    ... ))
    True
    >>> bool(_testFinalTablePadding(
    ...     sfntDirectorySize + sfntDirectoryEntrySize + 3,
    ...     1,
    ...     "test"
    ... ))
    True
    >>> bool(_testFinalTablePadding(
    ...     sfntDirectorySize + sfntDirectoryEntrySize + 4,
    ...     1,
    ...     "test"
    ... ))
    False
    """
    errors = []
    if (dataLength - (sfntDirectorySize + (sfntDirectoryEntrySize * numTables))) % 4:
        errors.append("The final table (%s) is not properly padded." % finalTableTag)
    return errors

def _testGaps(tableDirectory):
    """
    >>> start = sfntDirectorySize + (sfntDirectoryEntrySize * 2)
    >>> test = [
    ...     dict(offset=start, length=1, tag="test1"),
    ...     dict(offset=start+2, length=1, tag="test2"),
    ... ]
    >>> bool(_testGaps(test))
    False
    >>> test = [
    ...     dict(offset=start, length=1, tag="test1"),
    ...     dict(offset=start+3, length=1, tag="test2"),
    ... ]
    >>> bool(_testGaps(test))
    False
    >>> test = [
    ...     dict(offset=start, length=1, tag="test1"),
    ...     dict(offset=start+4, length=1, tag="test2"),
    ... ]
    >>> bool(_testGaps(test))
    False
    >>> test = [
    ...     dict(offset=start, length=1, tag="test1"),
    ...     dict(offset=start+5, length=1, tag="test2"),
    ... ]
    >>> bool(_testGaps(test))
    True
    """
    errors = []
    sorter = []
    for entry in tableDirectory:
        sorter.append((entry["offset"], entry))
    prevTag = None
    prevEnd = None
    for offset, entry in sorted(sorter):
        length = entry["length"]
        tag = entry["tag"]
        if prevEnd is None:
            prevEnd = offset + length
            prevTag = tag
        else:
            if offset - prevEnd > 3:
                errors.append("Improper padding between the %s and %s tables." % (prevTag, tag))
            prevEnd = offset + length
            prevTag = tag
    return errors

def _testGapAfterFinalTable(dataLength, tableDirectory):
    """
    >>> start = sfntDirectorySize + (sfntDirectoryEntrySize * 2)
    >>> test = [
    ...     dict(offset=start, length=1, tag="test")
    ... ]
    >>> bool(_testGapAfterFinalTable(start + 1, test))
    False
    >>> start = sfntDirectorySize + (sfntDirectoryEntrySize * 2)
    >>> test = [
    ...     dict(offset=start, length=1, tag="test")
    ... ]
    >>> bool(_testGapAfterFinalTable(start + 2, test))
    False
    >>> start = sfntDirectorySize + (sfntDirectoryEntrySize * 2)
    >>> test = [
    ...     dict(offset=start, length=1, tag="test")
    ... ]
    >>> bool(_testGapAfterFinalTable(start + 3, test))
    False
    >>> start = sfntDirectorySize + (sfntDirectoryEntrySize * 2)
    >>> test = [
    ...     dict(offset=start, length=1, tag="test")
    ... ]
    >>> bool(_testGapAfterFinalTable(start + 4, test))
    False
    >>> start = sfntDirectorySize + (sfntDirectoryEntrySize * 2)
    >>> test = [
    ...     dict(offset=start, length=1, tag="test")
    ... ]
    >>> bool(_testGapAfterFinalTable(start + 5, test))
    True
    """
    errors = []
    sorter = []
    for entry in tableDirectory:
        sorter.append((entry["offset"], entry))
    entry = sorted(sorter)[-1]
    offset = entry[-1]["offset"]
    length = entry[-1]["length"]
    lastPosition = offset + length
    if dataLength - lastPosition > 3:
        errors.append("Improper padding at the end of the file.")
    return errors

def _testCheckSums(tableDirectory):
    """
    >>> data = "0" * 44
    >>> checkSum = calcTableChecksum("test", data)
    >>> test = [
    ...     dict(data=data, checkSum=checkSum, tag="test")
    ... ]
    >>> bool(_testCheckSums(test))
    False
    >>> test = [
    ...     dict(data=data, checkSum=checkSum+1, tag="test")
    ... ]
    >>> bool(_testCheckSums(test))
    True
    """
    errors = []
    for entry in tableDirectory:
        tag = entry["tag"]
        checkSum = entry["checkSum"]
        data = entry["data"]
        shouldBe = calcTableChecksum(tag, data)
        if checkSum != shouldBe:
            errors.append("Invalid checksum for the %s table." % tag)
    return errors


if __name__ == "__main__":
    import doctest
    doctest.testmod(verbose=False)
