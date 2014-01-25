"""Classes for individual JPEG 2000 boxes.

References
----------
.. [JP2K15444-1i] International Organization for Standardication.  ISO/IEC
   15444-1:2004 - Information technology -- JPEG 2000 image coding system:
   Core coding system

.. [JP2K15444-2m] International Organization for Standardication.  ISO/IEC
   15444-2:2004 - Information technology -- JPEG 2000 image coding system:
   Extensions
"""

# pylint: disable=C0302,R0903,R0913

import datetime
import math
import os
import pprint
import struct
import sys
import uuid
import warnings
import xml.etree.cElementTree as ET
if sys.hexversion < 0x02070000:
    # pylint: disable=F0401,E0611
    from ordereddict import OrderedDict
    from xml.etree.cElementTree import XMLParserError as ParseError
else:
    from xml.etree.cElementTree import ParseError
    from collections import OrderedDict

import numpy as np

from .codestream import Codestream
from .core import _COLORSPACE_MAP_DISPLAY
from .core import _COLOR_TYPE_MAP_DISPLAY
from .core import ENUMERATED_COLORSPACE, RESTRICTED_ICC_PROFILE
from .core import ANY_ICC_PROFILE, VENDOR_COLOR_METHOD
from .core import _pretty_print_xml

from . import _uuid_io

_METHOD_DISPLAY = {
    ENUMERATED_COLORSPACE: 'enumerated colorspace',
    RESTRICTED_ICC_PROFILE: 'restricted ICC profile',
    ANY_ICC_PROFILE: 'any ICC profile',
    VENDOR_COLOR_METHOD: 'vendor color method'}

_APPROX_DISPLAY = {1: 'accurately represents correct colorspace definition',
                   2: 'approximates correct colorspace definition, '
                      + 'exceptional quality',
                   3: 'approximates correct colorspace definition, '
                      + 'reasonable quality',
                   4: 'approximates correct colorspace definition, '
                      + 'poor quality'}


class Jp2kBox(object):
    """Superclass for JPEG 2000 boxes.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    """

    def __init__(self, box_id='', offset=0, length=0, longname=''):
        self.box_id = box_id
        self.length = length
        self.offset = offset
        self.longname = longname

    def __repr__(self):
        msg = "glymur.jp2box.Jp2kBox(box_id='{0}', offset={1}, length={2}, "
        msg += "longname='{3}')"
        msg = msg.format(self.box_id, self.offset, self.length, self.longname)
        return msg

    def __str__(self):
        msg = "{0} Box ({1})".format(self.longname, self.box_id)
        msg += " @ ({0}, {1})".format(self.offset, self.length)
        return msg

    def write(self, _):
        """Must be implemented in a subclass.
        """
        msg = "Not supported for {0} box.".format(self.longname)
        raise NotImplementedError(msg)

    def parse_superbox(self, fptr):
        """Parse a superbox (box consisting of nothing but other boxes.

        Parameters
        ----------
        fptr : file
            Open file object.

        Returns
        -------
        List of top-level boxes in the JPEG 2000 file.
        """

        superbox = []

        start = fptr.tell()

        while True:

            # Are we at the end of the superbox?
            if start >= self.offset + self.length:
                break

            read_buffer = fptr.read(8)
            (box_length, box_id) = struct.unpack('>I4s', read_buffer)
            if sys.hexversion >= 0x03000000:
                box_id = box_id.decode('utf-8')

            if box_length == 0:
                # The length of the box is presumed to last until the end of
                # the file.  Compute the effective length of the box.
                num_bytes = os.path.getsize(fptr.name) - fptr.tell() + 8

            elif box_length == 1:
                # The length of the box is in the XL field, a 64-bit value.
                read_buffer = fptr.read(8)
                num_bytes, = struct.unpack('>Q', read_buffer)

            else:
                num_bytes = box_length

            # Call the proper parser for the given box with ID "T".
            try:
                box = _BOX_WITH_ID[box_id].parse(fptr, start, num_bytes)
            except KeyError:
                msg = 'Unrecognized box ({0}) encountered.'.format(box_id)
                warnings.warn(msg)
                box = Jp2kBox(box_id, offset=start, length=num_bytes,
                              longname='Unknown box')

            superbox.append(box)

            # Position to the start of the next box.
            if num_bytes > self.length:
                # Length of the current box goes past the end of the
                # enclosing superbox.
                msg = '{0} box has incorrect box length ({1})'
                msg = msg.format(box_id, num_bytes)
                warnings.warn(msg)
            elif fptr.tell() > start + num_bytes:
                # The box must be invalid somehow, as the file pointer is
                # positioned past the end of the box.
                msg = '{0} box may be invalid, the file pointer is positioned '
                msg += '{1} bytes past the end of the box.'
                msg = msg.format(box_id, fptr.tell() - (start + num_bytes))
                warnings.warn(msg)
            fptr.seek(start + num_bytes)

            start += num_bytes

        return superbox


class ColourSpecificationBox(Jp2kBox):
    """Container for JPEG 2000 color specification box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        Length of the box in bytes.
    offset : int
        Offset of the box from the start of the file.
    longname : str
        More verbose description of the box.
    method : int
        Method for defining the colorspace.
    precedence : int
        How this box ranks in priority compared to other color specification
        boxes.
    approximation : int
        Measure of colorspace accuracy.
    colorspace : int or None
        Enumerated colorspace, corresponds to one of 'sRGB', 'greyscale', or
        'YCC'.  If not None, then icc_profile must be None.
    icc_profile : dict
        ICC profile header according to ICC profile specification.  If
        colorspace is not None, then icc_profile must be empty.
    """

    def __init__(self, method=ENUMERATED_COLORSPACE, precedence=0,
                 approximation=0, colorspace=None, icc_profile=None,
                 length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='colr', longname='Colour Specification')

        if colorspace is not None and icc_profile is not None:
            raise IOError("colorspace and icc_profile cannot both be set.")
        if method not in (1, 2, 3, 4):
            raise IOError("Invalid method.")
        if approximation not in (0, 1, 2, 3, 4):
            raise IOError("Invalid approximation.")
        self.method = method
        self.precedence = precedence
        self.approximation = approximation
        self.colorspace = colorspace
        self.icc_profile = icc_profile
        self.length = length
        self.offset = offset

    def __repr__(self):
        msg = "glymur.jp2box.ColourSpecificationBox("
        msg += "method={0}, precedence={1}, approximation={2}, colorspace={3}, "
        msg += "icc_profile={4})"
        msg = msg.format(self.method,
                         self.precedence,
                         self.approximation,
                         self.colorspace,
                         self.icc_profile)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)

        msg += '\n    Method:  {0}'.format(_METHOD_DISPLAY[self.method])
        msg += '\n    Precedence:  {0}'.format(self.precedence)

        if self.approximation is not 0:
            dispvalue = _APPROX_DISPLAY[self.approximation]
            msg += '\n    Approximation:  {0}'.format(dispvalue)

        if self.colorspace is not None:
            dispvalue = _COLORSPACE_MAP_DISPLAY[self.colorspace]
            msg += '\n    Colorspace:  {0}'.format(dispvalue)
        else:
            # 2.7 has trouble pretty-printing ordered dicts so we just have
            # to print as a regular dict in this case.
            if sys.hexversion < 0x03000000:
                icc_profile = dict(self.icc_profile)
            else:
                icc_profile = self.icc_profile
            dispvalue = pprint.pformat(icc_profile)
            lines = [' ' * 8 + y for y in dispvalue.split('\n')]
            msg += '\n    ICC Profile:\n{0}'.format('\n'.join(lines))

        return msg

    def write(self, fptr):
        """Write an Colour Specification box to file.
        """
        if self.colorspace is None:
            msg = "Writing Colour Specification boxes without enumerated "
            msg += "colorspaces is not supported at this time."
            raise NotImplementedError(msg)
        length = 15 if self.icc_profile is None else 11 + len(self.icc_profile)
        fptr.write(struct.pack('>I', length))
        fptr.write('colr'.encode())

        read_buffer = struct.pack('>BBBI',
                                  self.method,
                                  self.precedence,
                                  self.approximation,
                                  self.colorspace)
        fptr.write(read_buffer)

    @staticmethod
    def parse(fptr, offset, length):
        """Parse JPEG 2000 color specification box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        ColourSpecificationBox instance
        """
        # Read the brand, minor version.
        read_buffer = fptr.read(3)
        (method, precedence, approximation) = struct.unpack('>BBB',
                                                            read_buffer)

        if method == 1:
            # enumerated colour space
            read_buffer = fptr.read(4)
            colorspace, = struct.unpack('>I', read_buffer)
            icc_profile = None

        else:
            # ICC profile
            colorspace = None
            numbytes = offset + length - fptr.tell()
            if numbytes < 128:
                msg = "ICC profile header is corrupt, length is "
                msg += "only {0} instead of 128."
                warnings.warn(msg.format(numbytes), UserWarning)
                icc_profile = None
            else:
                profile = _ICCProfile(fptr.read(numbytes))
                icc_profile = profile.header

        box = ColourSpecificationBox(method=method,
                                     precedence=precedence,
                                     approximation=approximation,
                                     colorspace=colorspace,
                                     icc_profile=icc_profile,
                                     length=length,
                                     offset=offset)
        return box


class _ICCProfile(object):
    """
    Container for ICC profile information.
    """
    profile_class = {b'scnr': 'input device profile',
                     b'mntr': 'display device profile',
                     b'prtr': 'output device profile',
                     b'link': 'devicelink profile',
                     b'spac': 'colorspace conversion profile',
                     b'abst': 'abstract profile',
                     b'nmcl': 'name colour profile'}

    colour_space_dict = {b'XYZ ': 'XYZ',
                         b'Lab ': 'Lab',
                         b'Luv ': 'Luv',
                         b'YCbr': 'YCbCr',
                         b'Yxy ': 'Yxy',
                         b'RGB ': 'RGB',
                         b'GRAY': 'gray',
                         b'HSV ': 'hsv',
                         b'HLS ': 'hls',
                         b'CMYK': 'CMYK',
                         b'CMY ': 'cmy',
                         b'2CLR': '2colour',
                         b'3CLR': '3colour',
                         b'4CLR': '4colour',
                         b'5CLR': '5colour',
                         b'6CLR': '6colour',
                         b'7CLR': '7colour',
                         b'8CLR': '8colour',
                         b'9CLR': '9colour',
                         b'ACLR': '10colour',
                         b'BCLR': '11colour',
                         b'CCLR': '12colour',
                         b'DCLR': '13colour',
                         b'ECLR': '14colour',
                         b'FCLR': '15colour'}

    rendering_intent_dict = {0: 'perceptual',
                             1: 'media-relative colorimetric',
                             2: 'saturation',
                             3: 'ICC-absolute colorimetric'}

    def __init__(self, read_buffer):
        self._raw_buffer = read_buffer
        header = OrderedDict()

        data = struct.unpack('>IIBB', self._raw_buffer[0:10])
        header['Size'] = data[0]
        header['Preferred CMM Type'] = data[1]
        major = data[2]
        minor = (data[3] & 0xf0) >> 4
        bugfix = (data[3] & 0x0f)
        header['Version'] = '{0}.{1}.{2}'.format(major, minor, bugfix)

        header['Device Class'] = self.profile_class[self._raw_buffer[12:16]]
        header['Color Space'] = self.colour_space_dict[self._raw_buffer[16:20]]
        data = self.colour_space_dict[self._raw_buffer[20:24]]
        header['Connection Space'] = data

        data = struct.unpack('>HHHHHH', self._raw_buffer[24:36])
        header['Datetime'] = datetime.datetime(data[0], data[1], data[2],
                                               data[3], data[4], data[5])
        header['File Signature'] = read_buffer[36:40].decode('utf-8')
        if read_buffer[40:44] == b'\x00\x00\x00\x00':
            header['Platform'] = 'unrecognized'
        else:
            header['Platform'] = read_buffer[40:44].decode('utf-8')

        fval, = struct.unpack('>I', read_buffer[44:48])
        flags = "{0}embedded, {1} be used independently"
        header['Flags'] = flags.format('' if fval & 0x01 else 'not ',
                                       'cannot' if fval & 0x02 else 'can')

        header['Device Manufacturer'] = read_buffer[48:52].decode('utf-8')
        if read_buffer[52:56] == b'\x00\x00\x00\x00':
            device_model = ''
        else:
            device_model = read_buffer[52:56].decode('utf-8')
        header['Device Model'] = device_model

        val, = struct.unpack('>Q', read_buffer[56:64])
        attr = "{0}, {1}, {2} media polarity, {3} media"
        attr = attr.format('transparency' if val & 0x01 else 'reflective',
                           'matte' if val & 0x02 else 'glossy',
                           'negative' if val & 0x04 else 'positive',
                           'black and white' if val & 0x08 else 'color')
        header['Device Attributes'] = attr

        rval, = struct.unpack('>I', read_buffer[64:68])
        try:
            header['Rendering Intent'] = self.rendering_intent_dict[rval]
        except KeyError:
            header['Rendering Intent'] = 'unknown'

        data = struct.unpack('>iii', read_buffer[68:80])
        header['Illuminant'] = np.array(data, dtype=np.float64) / 65536

        if read_buffer[80:84] == b'\x00\x00\x00\x00':
            creator = 'unrecognized'
        else:
            creator = read_buffer[80:84].decode('utf-8')
        header['Creator'] = creator

        if header['Version'][0] == '4':
            header['Profile Id'] = read_buffer[84:100]

        # Final 27 bytes are reserved.

        self.header = header


class ChannelDefinitionBox(Jp2kBox):
    """Container for component definition box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : numeric scalar
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    index : list
        number of the channel.  Defaults to monotonically increasing sequence,
        i.e. [0, 1, 2, ...]
    channel_type : list
        type of the channel
    association : list
        index of the associated color
    """
    def __init__(self, index=None, channel_type=None, association=None,
                 **kwargs):
        Jp2kBox.__init__(self, box_id='cdef', longname='Channel Definition')

        # channel type and association must be specified.
        if channel_type is None or association is None:
            raise IOError("channel_type and association must be specified.")

        if index is None:
            index = list(range(len(channel_type)))

        if len(index) != len(channel_type) or len(index) != len(association):
            msg = "Length of channel definition box inputs must be the same."
            raise IOError(msg)

        # channel types must be one of 0, 1, 2, 65535
        if any(x not in [0, 1, 2, 65535] for x in channel_type):
            msg = "Channel types must be in the set of\n\n"
            msg += "    0     - colour image data for associated color\n"
            msg += "    1     - opacity\n"
            msg += "    2     - premultiplied opacity\n"
            msg += "    65535 - unspecified"
            raise IOError(msg)

        self.index = tuple(index)
        self.channel_type = tuple(channel_type)
        self.association = tuple(association)
        self.__dict__.update(**kwargs)

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        for j in range(len(self.association)):
            color_type_string = _COLOR_TYPE_MAP_DISPLAY[self.channel_type[j]]
            if self.association[j] == 0:
                assn = 'whole image'
            else:
                assn = str(self.association[j])
            msg += '\n    Channel {0} ({1}) ==> ({2})'
            msg = msg.format(self.index[j], color_type_string, assn)
        return msg

    def __repr__(self):
        msg = "glymur.jp2box.ChannelDefinitionBox("
        msg += "index={0}, channel_type={1}, association={2})"
        msg = msg.format(self.index, self.channel_type, self.association)
        return msg

    def write(self, fptr):
        """Write a channel definition box to file.
        """
        num_components = len(self.association)
        fptr.write(struct.pack('>I', 8 + 2 + num_components * 6))
        fptr.write('cdef'.encode('utf-8'))
        fptr.write(struct.pack('>H', num_components))
        for j in range(num_components):
            fptr.write(struct.pack('>' + 'H' * 3,
                                   self.index[j],
                                   self.channel_type[j],
                                   self.association[j]))

    @staticmethod
    def parse(fptr, offset, length):
        """Parse component definition box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        ComponentDefinitionBox instance
        """
        # Read the number of components.
        read_buffer = fptr.read(2)
        num_components, = struct.unpack('>H', read_buffer)

        read_buffer = fptr.read(num_components * 6)
        data = struct.unpack('>' + 'HHH' * num_components, read_buffer)
        index = data[0:num_components * 6:3]
        channel_type = data[1:num_components * 6:3]
        association = data[2:num_components * 6:3]

        box = ChannelDefinitionBox(index=index, channel_type=channel_type,
                                   association=association, length=length,
                                   offset=offset)
        return box


class CodestreamHeaderBox(Jp2kBox):
    """Container for codestream header box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    box : list
        List of boxes contained in this superbox.
    """
    def __init__(self, box=None, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='jpch', longname='Codestream Header')
        self.length = length
        self.offset = offset
        self.box = box if box is not None else []

    def __repr__(self):
        msg = "glymur.jp2box.CodestreamHeaderBox(box={0})".format(self.box)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        for box in self.box:
            boxstr = str(box)

            # Add indentation.
            strs = [('\n    ' + x) for x in boxstr.split('\n')]
            msg += ''.join(strs)
        return msg

    @staticmethod
    def parse(fptr, offset, length):
        """Parse codestream header box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        CodestreamHeaderBox instance
        """
        box = CodestreamHeaderBox(length=length, offset=offset)

        # The codestream header box is a superbox, so go ahead and parse its
        # child boxes.
        box.box = box.parse_superbox(fptr)

        return box


class CompositingLayerHeaderBox(Jp2kBox):
    """Container for compositing layer header box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    box : list
        List of boxes contained in this superbox.
    """
    def __init__(self, box=None, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='jplh',
                         longname='Compositing Layer Header')
        self.length = length
        self.offset = offset
        self.box = box if box is not None else []

    def __repr__(self):
        msg = "glymur.jp2box.CompositingLayerHeaderBox(box={0})"
        msg = msg.format(self.box)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        for box in self.box:
            boxstr = str(box)

            # Add indentation.
            strs = [('\n    ' + x) for x in boxstr.split('\n')]
            msg += ''.join(strs)
        return msg

    @staticmethod
    def parse(fptr, offset, length):
        """Parse compositing layer header box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        CompositingLayerHeaderBox instance
        """
        box = CompositingLayerHeaderBox(length=length, offset=offset)

        # This box is a superbox, so go ahead and parse its # child boxes.
        box.box = box.parse_superbox(fptr)

        return box


class ComponentMappingBox(Jp2kBox):
    """Container for component mapping information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : numeric scalar
        Length of the box in bytes.
    offset : int
        Offset of the box from the start of the file.
    longname : str
        Verbose description of the box.
    component_index : tuple
        Index of component in codestream that is mapped to this channel.
    mapping_type : tuple
        mapping type, either direct use (0) or palette (1)
    palette_index : tuple
        Index component from palette
    """
    def __init__(self, component_index, mapping_type, palette_index,
                 length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='cmap', longname='Component Mapping')
        self.component_index = component_index
        self.mapping_type = mapping_type
        self.palette_index = palette_index
        self.length = length
        self.offset = offset

    def __repr__(self):
        msg = "glymur.jp2box.ComponentMappingBox("
        msg += "component_index={0}, mapping_type={1}, palette_index={2})"
        msg = msg.format(self.component_index,
                         self.mapping_type,
                         self.palette_index)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)

        for k in range(len(self.component_index)):
            if self.mapping_type[k] == 1:
                msg += '\n    Component {0} ==> palette column {1}'
                msg = msg.format(self.component_index[k],
                                 self.palette_index[k])
            else:
                msg += '\n    Component %d ==> %d'
                msg = msg.format(self.component_index[k], k)
        return msg

    @staticmethod
    def parse(fptr, offset, length):
        """Parse component mapping box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        ComponentMappingBox instance
        """
        num_bytes = offset + length - fptr.tell()
        num_components = int(num_bytes/4)

        read_buffer = fptr.read(num_bytes)
        data = struct.unpack('>' + 'HBB' * num_components, read_buffer)

        component_index = data[0:num_bytes:num_components]
        mapping_type = data[1:num_bytes:num_components]
        palette_index = data[2:num_bytes:num_components]

        box = ComponentMappingBox(component_index, mapping_type, palette_index,
                                  length=length, offset=offset)
        return box


class ContiguousCodestreamBox(Jp2kBox):
    """Container for JPEG2000 codestream information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    main_header : list
        List of segments in the codestream header.
    """
    def __init__(self, main_header=None, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='jp2c', longname='Contiguous Codestream')
        self.main_header = main_header
        self.length = length
        self.offset = offset

    def __repr__(self):
        msg = "glymur.jp2box.ContiguousCodeStreamBox(main_header={0})"
        return msg.format(repr(self.main_header))

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        msg += '\n    Main header:'
        for segment in self.main_header.segment:
            segstr = str(segment)

            # Add indentation.
            strs = [('\n        ' + x) for x in segstr.split('\n')]
            msg += ''.join(strs)
        return msg

    @staticmethod
    def parse(fptr, offset=0, length=0):
        """Parse a codestream box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        ContiguousCodestreamBox instance
        """
        main_header = Codestream(fptr, length, header_only=True)
        box = ContiguousCodestreamBox(main_header, length=length,
                                      offset=offset)
        return box


class FileTypeBox(Jp2kBox):
    """Container for JPEG 2000 file type box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    brand: str
        Specifies the governing recommendation or standard upon which this
        file is based.
    minor_version: int
        Minor version number identifying the JP2 specification used.
    compatibility_list: list
        List of file conformance profiles.
    """
    def __init__(self, brand='jp2 ', minor_version=0,
                 compatibility_list=None, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='ftyp', longname='File Type')
        self.brand = brand
        self.minor_version = minor_version
        if compatibility_list is None:
            # see W0102, pylint
            self.compatibility_list = ['jp2 ']
        else:
            self.compatibility_list = compatibility_list
        self.length = length
        self.offset = offset

    def __repr__(self):
        msg = "glymur.jp2box.FileTypeBox(brand='{0}', minor_version={1}, "
        msg += "compatibility_list={2})"
        msg = msg.format(self.brand, self.minor_version,
                         self.compatibility_list)
        return msg

    def __str__(self):
        lst = [Jp2kBox.__str__(self),
               '    Brand:  {0}',
               '    Compatibility:  {1}']
        msg = '\n'.join(lst)
        msg = msg.format(self.brand, self.compatibility_list)

        return msg

    def write(self, fptr):
        """Write a File Type box to file.
        """
        length = 16 + 4*len(self.compatibility_list)
        fptr.write(struct.pack('>I', length))
        fptr.write('ftyp'.encode())
        fptr.write(self.brand.encode())
        fptr.write(struct.pack('>I', self.minor_version))

        for item in self.compatibility_list:
            fptr.write(item.encode())

    @staticmethod
    def parse(fptr, offset, length):
        """Parse JPEG 2000 file type box.

        Parameters
        ----------
        f : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        FileTypeBox instance
        """
        # Read the brand, minor version.
        read_buffer = fptr.read(8)
        (brand, minor_version) = struct.unpack('>4sI', read_buffer)
        if sys.hexversion >= 0x030000:
            brand = brand.decode('utf-8')

        # Read the compatibility list.  Each entry has 4 bytes.
        current_pos = fptr.tell()
        num_bytes = (offset + length - current_pos) / 4
        read_buffer = fptr.read(int(num_bytes) * 4)
        compatibility_list = []
        for j in range(int(num_bytes)):
            entry, = struct.unpack('>4s', read_buffer[4*j:4*(j+1)])
            if sys.hexversion >= 0x03000000:
                entry = entry.decode('utf-8')
            compatibility_list.append(entry)

        compatibility_list = compatibility_list

        box = FileTypeBox(brand=brand, minor_version=minor_version,
                          compatibility_list=compatibility_list,
                          length=length, offset=offset)
        return box


class ImageHeaderBox(Jp2kBox):
    """Container for JPEG 2000 image header box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    height, width :  int
        Height and width of image.
    num_components : int
        Number of image channels.
    bits_per_component : int
        Bits per component.
    signed : bool
        False if the image components are unsigned.
    compression : int
        The compression type, should be 7 if JP2.
    colorspace_unknown : bool
        False if the color space is known and correctly specified.
    ip_provided : bool
        False if the file does not contain intellectual propery rights
        information.
    """
    def __init__(self, height, width, num_components=1, signed=False,
                 bits_per_component=8, compression=7, colorspace_unknown=False,
                 ip_provided=False, length=0, offset=-1):
        """
        Examples
        --------
        >>> import glymur
        >>> box = glymur.jp2box.ImageHeaderBox(height=512, width=256)
        """
        Jp2kBox.__init__(self, box_id='ihdr', longname='Image Header')
        self.height = height
        self.width = width
        self.num_components = num_components
        self.signed = signed
        self.bits_per_component = bits_per_component
        self.compression = compression
        self.colorspace_unknown = colorspace_unknown
        self.ip_provided = ip_provided
        self.length = length
        self.offset = offset

    def __repr__(self):
        msg = "glymur.jp2box.ImageHeaderBox("
        msg += "{height}, {width}, num_components={num_components}, "
        msg += "signed={signed}, bits_per_component={bits_per_component}, "
        msg += "compression={compression}, "
        msg += "colorspace_unknown={colorspace_unknown}, "
        msg += "ip_provided={ip_provided})"
        msg = msg.format(height=self.height, width=self.width,
                         num_components=self.num_components,
                         signed=self.signed,
                         bits_per_component=self.bits_per_component,
                         compression=self.compression,
                         colorspace_unknown=self.colorspace_unknown,
                         ip_provided=self.ip_provided)
        return msg

    def __str__(self):
        msg = "{0}"
        msg += '\n    Size:  [{1} {2} {3}]'
        msg += '\n    Bitdepth:  {4}'
        msg += '\n    Signed:  {5}'
        msg += '\n    Compression:  {6}'
        msg += '\n    Colorspace Unknown:  {7}'
        msg = msg.format(Jp2kBox.__str__(self),
                         self.height, self.width, self.num_components,
                         self.bits_per_component,
                         self.signed,
                         'wavelet' if self.compression == 7 else 'unknown',
                         self.colorspace_unknown)
        return msg

    def write(self, fptr):
        """Write an Image Header box to file.
        """
        fptr.write(struct.pack('>I', 22))
        fptr.write('ihdr'.encode())

        # signedness and bps are stored together in a single byte
        bit_depth_signedness = 0x80 if self.signed else 0x00
        bit_depth_signedness |= self.bits_per_component - 1
        read_buffer = struct.pack('>IIHBBBB',
                                  self.height,
                                  self.width,
                                  self.num_components,
                                  bit_depth_signedness,
                                  self.compression,
                                  1 if self.colorspace_unknown else 0,
                                  1 if self.ip_provided else 0)
        fptr.write(read_buffer)

    @staticmethod
    def parse(fptr, offset, length):
        """Parse JPEG 2000 image header box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        ImageHeaderBox instance
        """
        # Read the box information
        read_buffer = fptr.read(14)
        params = struct.unpack('>IIHBBBB', read_buffer)
        height = params[0]
        width = params[1]
        num_components = params[2]
        bits_per_component = (params[3] & 0x7f) + 1
        signed = (params[3] & 0x80) > 1
        compression = params[4]
        colorspace_unknown = True if params[5] else False
        ip_provided = True if params[6] else False

        box = ImageHeaderBox(height, width, num_components=num_components,
                             bits_per_component=bits_per_component,
                             signed=signed,
                             compression=compression,
                             colorspace_unknown=colorspace_unknown,
                             ip_provided=ip_provided,
                             length=length, offset=offset)
        return box


class AssociationBox(Jp2kBox):
    """Container for Association box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    box : list
        List of boxes contained in this superbox.
    """
    def __init__(self, box=None, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='asoc', longname='Association')
        self.length = length
        self.offset = offset
        self.box = box if box is not None else []

    def __repr__(self):
        msg = "glymur.jp2box.AssociationBox(box={0})".format(self.box)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        for box in self.box:
            boxstr = str(box)

            # Add indentation.
            strs = [('\n    ' + x) for x in boxstr.split('\n')]
            msg += ''.join(strs)
        return msg

    @staticmethod
    def parse(fptr, offset, length):
        """Parse association box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        AssociationBox instance
        """
        box = AssociationBox(length=length, offset=offset)

        # The Association box is a superbox, so go ahead and parse its child
        # boxes.
        box.box = box.parse_superbox(fptr)

        return box


class JP2HeaderBox(Jp2kBox):
    """Container for JP2 header box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    box : list
        List of boxes contained in this superbox.
    """
    def __init__(self, box=None, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='jp2h', longname='JP2 Header')
        self.length = length
        self.offset = offset
        self.box = box if box is not None else []

    def __repr__(self):
        msg = "glymur.jp2box.JP2HeaderBox(box={0})".format(self.box)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        for box in self.box:
            boxstr = str(box)

            # Add indentation.
            strs = [('\n    ' + x) for x in boxstr.split('\n')]
            msg += ''.join(strs)
        return msg

    def write(self, fptr):
        """Write a JP2 Header box to file.
        """
        # Write the contained boxes, then come back and write the length.
        orig_pos = fptr.tell()
        fptr.write(struct.pack('>I', 0))
        fptr.write('jp2h'.encode())
        for box in self.box:
            box.write(fptr)

        end_pos = fptr.tell()
        fptr.seek(orig_pos)
        fptr.write(struct.pack('>I', end_pos - orig_pos))
        fptr.seek(end_pos)

    @staticmethod
    def parse(fptr, offset, length):
        """Parse JPEG 2000 header box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        JP2HeaderBox instance
        """
        box = JP2HeaderBox(length=length, offset=offset)

        # The JP2 header box is a superbox, so go ahead and parse its child
        # boxes.
        box.box = box.parse_superbox(fptr)

        return box


class JPEG2000SignatureBox(Jp2kBox):
    """Container for JPEG 2000 signature box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    signature : tuple
        Four-byte tuple identifying the file as JPEG 2000.
    """
    def __init__(self, signature=(13, 10, 135, 10), length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='jP  ', longname='JPEG 2000 Signature')
        self.signature = signature
        self.length = length
        self.offset = offset

    def __repr__(self):
        return 'glymur.jp2box.JPEG2000SignatureBox()'

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        msg += '\n    Signature:  {0:02x}{1:02x}{2:02x}{3:02x}'
        msg = msg.format(self.signature[0], self.signature[1],
                         self.signature[2], self.signature[3])
        return msg

    def write(self, fptr):
        """Write a JPEG 2000 Signature box to file.
        """
        fptr.write(struct.pack('>I', 12))
        fptr.write(self.box_id.encode())
        fptr.write(struct.pack('>BBBB', *self.signature))

    @staticmethod
    def parse(fptr, offset, length):
        """Parse JPEG 2000 signature box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        JPEG2000SignatureBox instance
        """
        read_buffer = fptr.read(4)
        signature = struct.unpack('>BBBB', read_buffer)

        box = JPEG2000SignatureBox(signature=signature, length=length,
                                   offset=offset)
        return box


class PaletteBox(Jp2kBox):
    """Container for palette box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    palette : ndarray
        Colormap array.
    """
    def __init__(self, palette, bits_per_component, signed, length=0,
                 offset=-1):
        Jp2kBox.__init__(self, box_id='pclr', longname='Palette')
        self.palette = palette
        self.bits_per_component = bits_per_component
        self.signed = signed
        self.length = length
        self.offset = offset

    def __repr__(self):
        msg = "glymur.jp2box.PaletteBox({0}, bits_per_component={1}, "
        msg += "signed={2})"
        msg = msg.format(self.palette, self.bits_per_component, self.signed)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        msg += '\n    Size:  ({0} x {1})'.format(*self.palette.shape)
        return msg

    @staticmethod
    def parse(fptr, offset, length):
        """Parse palette box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        PaletteBox instance
        """
        # Get the size of the palette.
        read_buffer = fptr.read(3)
        (num_entries, num_columns) = struct.unpack('>HB', read_buffer)

        # Need to determine bps and signed or not
        read_buffer = fptr.read(num_columns)
        data = struct.unpack('>' + 'B' * num_columns, read_buffer)
        bps = [((x & 0x07f) + 1) for x in data]
        signed = [((x & 0x80) > 1) for x in data]

        fmt = '>'
        for bits in bps:
            if bits <= 8:
                fmt += 'B'
            elif bits <= 16:
                fmt += 'H'
            elif bits <= 32:
                fmt += 'I'

        # Each palette component is padded out to the next largest byte.
        # That means a list comprehension does this in one shot.
        row_nbytes = sum([int(math.ceil(x/8.0)) for x in bps])

        read_buffer = fptr.read(num_entries * row_nbytes)
        palette = np.zeros((num_entries, num_columns), dtype=np.int32)
        for j in range(num_entries):
            palette[j] = struct.unpack_from(fmt, read_buffer,
                                            offset=j * row_nbytes)

        box = PaletteBox(palette, bps, signed, length=length, offset=offset)
        return box


# Map rreq codes to display text.
_READER_REQUIREMENTS_DISPLAY = {
    0:  'File not completely understood',
    1:  'Deprecated',
    2:  'Contains multiple composition layers',
    3:  'Deprecated',
    4:  'JPEG 2000 Part 1 Profile 1 codestream',
    5:  'Unrestricted JPEG 2000 Part 1 codestream, ITU-T Rec. T.800 '
        + '| ISO/IEC 15444-1',
    6:  'Unrestricted JPEG 2000 Part 2 codestream',
    7:  'JPEG codestream as defined in ISO/IEC 10918-1',
    8:  'Deprecated',
    9:  'Non-premultiplied opacity channel',
    10:  'Premultiplied opacity channel',
    11:  'Chroma-key based opacity',
    12:  'Deprecated',
    13:  'Fragmented codestream where all fragments are in file and in order',
    14:  'Fragmented codestream where all fragments are in file '
         + 'but are out of order',
    15:  'Fragmented codestream where not all fragments are within the file '
         + 'but are all in locally accessible files',
    16:  'Fragmented codestream where some fragments may be accessible '
         + 'only through a URL specified network connection',
    17:  'Compositing required to produce rendered result from multiple '
         + 'compositing layers',
    18:  'Deprecated',
    19:  'Deprecated',
    20:  'Deprecated',
    21:  'At least one compositing layer consists of multiple codestreams',
    22:  'Deprecated',
    23:  'Colourspace transformations are required to combine compositing '
         + 'layers; not all compositing layers are in the same colourspace',
    24:  'Deprecated',
    25:  'Deprecated',
    26:  'First animation layer does not cover entire rendered result',
    27:  'Deprecated',
    28:  'Reuse of animation layers',
    29:  'Deprecated',
    30:  'Some animated frames are non-persistent',
    31:  'Deprecated',
    32:  'Rendered result involves scaling within a layer',
    33:  'Rendered result involves scaling between layers',
    34:  'ROI metadata',
    35:  'IPR metadata',
    36:  'Content metadata',
    37:  'History metadata',
    38:  'Creation metadata',
    39:  'JPX digital signatures',
    40:  'JPX checksums',
    41:  'Desires Graphics Arts Reproduction specified',
    42:  'Deprecated',
    43:  '(Deprecated) compositing layer uses restricted ICC profile',
    44:  'Compositing layer uses Any ICC profile',
    45:  'Deprecated',
    46:  'Deprecated',
    47:  'BiLevel 1 enumerated colourspace',
    48:  'BiLevel 2 enumerated colourspace',
    49:  'YCbCr 1 enumerated colourspace',
    50:  'YCbCr 2 enumerated colourspace',
    51:  'YCbCr 3 enumerated colourspace',
    52:  'PhotoYCC enumerated colourspace',
    53:  'YCCK enumerated colourspace',
    54:  'CMY enumerated colourspace',
    55:  'CMYK enumerated colorspace',
    56:  'CIELab enumerated colourspace with default parameters',
    57:  'CIELab enumerated colourspace with non-default parameters',
    58:  'CIEJab enumerated colourspace with default parameters',
    59:  'CIEJab enumerated colourspace with non-default parameters',
    60:  'e-sRGB enumerated colorspace',
    61:  'ROMM_RGB enumerated colorspace',
    62:  'Non-square samples',
    63:  'Deprecated',
    64:  'Deprecated',
    65:  'Deprecated',
    66:  'Deprecated',
    67:  'GIS metadata XML box',
    68:  'JPSEC extensions in codestream as specified by ISO/IEC 15444-8',
    69:  'JP3D extensions in codestream as specified by ISO/IEC 15444-10',
    70:  'Deprecated',
    71:  'e-sYCC enumerated colourspace',
    72:  'JPEG 2000 Part 2 codestream as restricted by baseline conformance '
         + 'requirements in M.9.2.3',
    73:  'YPbPr(1125/60) enumerated colourspace',
    74:  'YPbPr(1250/50) enumerated colourspace'}


class ReaderRequirementsBox(Jp2kBox):
    """Container for reader requirements box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    fuam : int
        Fully Understand Aspects mask.
    dcm : int
        Decode completely mask.
    standard_flag : list
        Integers specifying standard features.
    standard_mask : list
        Specifies  the compatibility mask for each corresponding standard
        flag.
    vendor_feature : list
        Each item is a UUID corresponding to a vendor defined feature.
    vendor_mask : list
        Specifies the compatibility mask for each corresponding vendor
        feature.
    """
    def __init__(self, fuam, dcm, standard_flag, standard_mask, vendor_feature,
                 vendor_mask, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='rreq', longname='Reader Requirements')
        self.fuam = fuam
        self.dcm = dcm
        self.standard_flag = standard_flag
        self.standard_mask = standard_mask
        self.vendor_feature = vendor_feature
        self.vendor_mask = vendor_mask
        self.length = length
        self.offset = offset

    def __repr__(self):
        msg = "glymur.jp2box.ReaderRequirementsBox(fuam={fuam}, dcm={dcm}, "
        msg += "standard_flag={standard_flag}, standard_mask={standard_mask}, "
        msg += "vendor_feature={vendor_feature}, vendor_mask={vendor_mask})"
        msg = msg.format(fuam=self.fuam,
                         dcm=self.dcm,
                         standard_flag=self.standard_flag,
                         standard_mask=self.standard_mask,
                         vendor_feature=self.vendor_feature,
                         vendor_mask=self.vendor_mask)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)

        msg += '\n    Standard Features:'
        for j in range(len(self.standard_flag)):
            sfl = self.standard_flag[j]
            rrdisp = _READER_REQUIREMENTS_DISPLAY[self.standard_flag[j]]
            msg += '\n        Feature {0:03d}:  {1}'.format(sfl, rrdisp)

        msg += '\n    Vendor Features:'
        for j in range(len(self.vendor_feature)):
            msg += '\n        UUID {0}'.format(self.vendor_feature[j])

        return msg

    @staticmethod
    def parse(fptr, offset, length):
        """Parse reader requirements box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        ReaderRequirementsBox instance
        """
        read_buffer = fptr.read(1)
        mask_length, = struct.unpack('>B', read_buffer)

        # Fully Understands Aspect Mask
        # Decodes Completely Mask
        read_buffer = fptr.read(2 * mask_length)

        # The mask length tells us the format string to use when unpacking
        # from the buffer read from file.
        mask_format = {1: 'B', 2: 'H', 4: 'I'}[mask_length]
        fuam, dcm = struct.unpack('>' + mask_format * 2, read_buffer)

        standard_flag, standard_mask = _parse_standard_flag(fptr, mask_length)
        vendor_feature, vendor_mask = _parse_vendor_features(fptr, mask_length)

        box = ReaderRequirementsBox(fuam, dcm, standard_flag, standard_mask,
                                    vendor_feature, vendor_mask,
                                    length=length, offset=offset)
        return box


def _parse_standard_flag(fptr, mask_length):
    """Construct standard flag, standard mask data from the file.

    Specifically working on Reader Requirements box.

    Parameters
    ----------
    fptr : file object
        File object for JP2K file.
    mask_length : int
        Length of standard mask flag
    """
    # The mask length tells us the format string to use when unpacking
    # from the buffer read from file.
    mask_format = {1: 'B', 2: 'H', 4: 'I'}[mask_length]

    read_buffer = fptr.read(2)
    num_standard_flags, = struct.unpack('>H', read_buffer)

    # Read in standard flags and standard masks.  Each standard flag should
    # be two bytes, but the standard mask flag is as long as specified by
    # the mask length.
    read_buffer = fptr.read(num_standard_flags * (2 + mask_length))

    fmt = '>' + ('H' + mask_format) * num_standard_flags
    data = struct.unpack(fmt, read_buffer)

    standard_flag = data[0:num_standard_flags * 2:2]
    standard_mask = data[1:num_standard_flags * 2:2]

    return standard_flag, standard_mask


def _parse_vendor_features(fptr, mask_length):
    """Construct vendor features, vendor mask data from the file.

    Specifically working on Reader Requirements box.

    Parameters
    ----------
    fptr : file object
        File object for JP2K file.
    mask_length : int
        Length of vendor mask flag
    """
    # The mask length tells us the format string to use when unpacking
    # from the buffer read from file.
    mask_format = {1: 'B', 2: 'H', 4: 'I'}[mask_length]

    read_buffer = fptr.read(2)
    num_vendor_features, = struct.unpack('>H', read_buffer)

    # Each vendor feature consists of a 16-byte UUID plus a mask whose
    # length is specified by, you guessed it, "mask_length".
    entry_length = 16 + mask_length
    read_buffer = fptr.read(num_vendor_features * entry_length)
    vendor_feature = []
    vendor_mask = []
    for j in range(num_vendor_features):
        ubuffer = read_buffer[j * entry_length:(j + 1) * entry_length]
        vendor_feature.append(uuid.UUID(bytes=ubuffer[0:16]))

        vmask = struct.unpack('>' + mask_format, ubuffer[16:])
        vendor_mask.append(vmask)

    return vendor_feature, vendor_mask


class ResolutionBox(Jp2kBox):
    """Container for Resolution superbox information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    box : list
        List of boxes contained in this superbox.
    """
    def __init__(self, box=None, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='res ', longname='Resolution')
        self.length = length
        self.offset = offset
        self.box = box if box is not None else []

    def __repr__(self):
        msg = "glymur.jp2box.ResolutionBox(box={0})"
        msg = msg.format(self.box)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        for box in self.box:
            boxstr = str(box)

            # Add indentation.
            strs = [('\n    ' + x) for x in boxstr.split('\n')]
            msg += ''.join(strs)
        return msg

    @staticmethod
    def parse(fptr, offset, length):
        """Parse Resolution box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        ResolutionBox instance
        """
        box = ResolutionBox(length=length, offset=offset)

        # The JP2 header box is a superbox, so go ahead and parse its child
        # boxes.
        box.box = box.parse_superbox(fptr)

        return box


class CaptureResolutionBox(Jp2kBox):
    """Container for Capture resolution box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    vertical_resolution, horizontal_resolution : float
        Vertical, horizontal resolution.
    """
    def __init__(self, vertical_resolution, horizontal_resolution, length=0,
                 offset=-1):
        Jp2kBox.__init__(self, box_id='resc', longname='Capture Resolution')
        self.vertical_resolution = vertical_resolution
        self.horizontal_resolution = horizontal_resolution
        self.length = length
        self.offset = offset

    def __repr__(self):
        msg = "glymur.jp2box.CaptureResolutionBox({0}, {1})"
        msg = msg.format(self.vertical_resolution, self.horizontal_resolution)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        msg += '\n    VCR:  {0}'.format(self.vertical_resolution)
        msg += '\n    HCR:  {0}'.format(self.horizontal_resolution)
        return msg

    @staticmethod
    def parse(fptr, offset, length):
        """Parse CaptureResolutionBox.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        CaptureResolutionBox instance
        """
        read_buffer = fptr.read(10)
        (rn1, rd1, rn2, rd2, re1, re2) = struct.unpack('>HHHHBB', read_buffer)
        vres = rn1 / rd1 * math.pow(10, re1)
        hres = rn2 / rd2 * math.pow(10, re2)

        box = CaptureResolutionBox(vres, hres, length=length, offset=offset)

        return box


class DisplayResolutionBox(Jp2kBox):
    """Container for Display resolution box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    vertical_resolution, horizontal_resolution : float
        Vertical, horizontal resolution.
    """
    def __init__(self, vertical_resolution, horizontal_resolution,
                 length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='resd', longname='Display Resolution')
        self.vertical_resolution = vertical_resolution
        self.horizontal_resolution = horizontal_resolution
        self.length = length
        self.offset = offset

    def __repr__(self):
        msg = "glymur.jp2box.DisplayResolutionBox({0}, {1})"
        msg = msg.format(self.vertical_resolution, self.horizontal_resolution)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        msg += '\n    VDR:  {0}'.format(self.vertical_resolution)
        msg += '\n    HDR:  {0}'.format(self.horizontal_resolution)
        return msg

    @staticmethod
    def parse(fptr, offset, length):
        """Parse display resolution box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        DisplayResolutionBox instance
        """

        read_buffer = fptr.read(10)
        (rn1, rd1, rn2, rd2, re1, re2) = struct.unpack('>HHHHBB', read_buffer)
        vres = rn1 / rd1 * math.pow(10, re1)
        hres = rn2 / rd2 * math.pow(10, re2)

        box = DisplayResolutionBox(vres, hres, length=length, offset=offset)

        return box


class LabelBox(Jp2kBox):
    """Container for Label box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    label : str
        Textual label.
    """
    def __init__(self, label, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='lbl ', longname='Label')
        self.label = label
        self.length = length
        self.offset = offset

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        msg += '\n    Label:  {0}'.format(self.label)
        return msg

    def __repr__(self):
        msg = 'glymur.jp2box.LabelBox("{0}")'.format(self.label)
        return msg

    @staticmethod
    def parse(fptr, offset, length):
        """Parse Label box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        LabelBox instance
        """
        num_bytes = offset + length - fptr.tell()
        read_buffer = fptr.read(num_bytes)
        label = read_buffer.decode('utf-8')
        box = LabelBox(label, length=length, offset=offset)
        return box


class XMLBox(Jp2kBox):
    """Container for XML box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    xml : ElementTree object
        XML section.
    """
    def __init__(self, xml=None, filename=None, length=0, offset=-1):
        """
        Parameters
        ----------
        xml : ElementTree
            An ElementTree object already existing in python.
        filename : str
            File from which to read XML.  If filename is not None, then the xml
            keyword argument must be None.
        """
        Jp2kBox.__init__(self, box_id='xml ', longname='XML')
        if filename is not None and xml is not None:
            msg = "Only one of either filename or xml should be provided."
            raise IOError(msg)
        if filename is not None:
            self.xml = ET.parse(filename)
        else:
            self.xml = xml
        self.length = length
        self.offset = offset

    def __repr__(self):
        return "glymur.jp2box.XMLBox(xml={0})".format(self.xml)

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        xml = self.xml
        if self.xml is not None:
            msg += _pretty_print_xml(self.xml)
        else:
            msg += '\n    {0}'.format(xml)
        return msg

    def write(self, fptr):
        """Write an XML box to file.
        """
        try:
            read_buffer = ET.tostring(self.xml, encoding='utf-8')
        except (AttributeError, AssertionError):
            # AssertionError on 2.6
            read_buffer = ET.tostring(self.xml.getroot(), encoding='utf-8')

        fptr.write(struct.pack('>I', len(read_buffer) + 8))
        fptr.write(self.box_id.encode())
        fptr.write(read_buffer)

    @staticmethod
    def parse(fptr, offset, length):
        """Parse XML box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        XMLBox instance
        """
        num_bytes = offset + length - fptr.tell()
        read_buffer = fptr.read(num_bytes)
        try:
            text = read_buffer.decode('utf-8')
        except UnicodeDecodeError as ude:
            # Possibly bad string of bytes to begin with.
            # Try to search for <?xml and go from there.
            decl_start = read_buffer.find(b'<?xml')
            if decl_start > -1:
                text = read_buffer[decl_start:].decode('utf-8')
            else:
                raise

            # Let the user know that the XML box was problematic.
            msg = 'A UnicodeDecodeError was encountered parsing an XML box at '
            msg += 'byte position {0} ({1}), but the XML was still recovered.'
            msg = msg.format(offset, ude.reason)
            warnings.warn(msg, UserWarning)

        # Strip out any trailing nulls, as they can foul up XML parsing.
        text = text.rstrip(chr(0))

        try:
            elt = ET.fromstring(text.encode('utf-8'))
            xml = ET.ElementTree(elt)
        except ParseError as parse_error:
            msg = 'A problem was encountered while parsing an XML box:'
            msg += '\n\n\t"{0}"\n\nNo XML was retrieved.'
            msg = msg.format(str(parse_error))
            warnings.warn(msg, UserWarning)
            xml = None

        box = XMLBox(xml=xml, length=length, offset=offset)
        return box


class UUIDListBox(Jp2kBox):
    """Container for JPEG 2000 UUID list box.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    ulst : list
        List of UUIDs.
    """
    def __init__(self, ulst, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='ulst', longname='UUID List')
        self.ulst = ulst
        self.length = length
        self.offset = offset

    def __repr__(self):
        msg = "glymur.jp2box.UUIDListBox({0})".format(self.ulst)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        for j, uuid_item in enumerate(self.ulst):
            msg += '\n    UUID[{0}]:  {1}'.format(j, uuid_item)
        return(msg)

    @staticmethod
    def parse(fptr, offset, length):
        """Parse UUIDList box.

        Parameters
        ----------
        f : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        UUIDListBox instance
        """
        read_buffer = fptr.read(2)
        num_uuids, = struct.unpack('>H', read_buffer)

        ulst = []
        for _ in range(num_uuids):
            read_buffer = fptr.read(16)
            ulst.append(uuid.UUID(bytes=read_buffer))

        box = UUIDListBox(ulst, length=length, offset=offset)
        return(box)


class UUIDInfoBox(Jp2kBox):
    """Container for JPEG 2000 UUID Info superbox.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    box : list
        List of boxes contained in this superbox.
    """
    def __init__(self, box=None, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='uinf', longname='UUIDInfo')
        self.length = length
        self.offset = offset
        self.box = box if box is not None else []

    def __repr__(self):
        msg = "glymur.jp2box.UUIDInfoBox(box={0})".format(self.box)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)

        for box in self.box:
            box_str = str(box)

            # Add indentation.
            lst = [('\n    ' + x) for x in box_str.split('\n')]
            msg += ''.join(lst)

        return(msg)

    @staticmethod
    def parse(fptr, offset, length):
        """Parse UUIDInfo super box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        UUIDInfoBox instance
        """

        box = UUIDInfoBox(length=length, offset=offset)

        # The UUIDInfo box is a superbox, so go ahead and parse its child
        # boxes.
        box.box = box.parse_superbox(fptr)

        return box


class DataEntryURLBox(Jp2kBox):
    """Container for data entry URL box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    version : byte
        Must be 0 for JP2.
    flag : bytes
        Particular attributes of this box, consists of three bytes.
    URL : str
        Associated URL.
    """
    def __init__(self, version, flag, url, length=0, offset=-1):
        Jp2kBox.__init__(self, box_id='url ', longname='Data Entry URL')
        self.version = version
        self.flag = flag
        self.url = url
        self.length = length
        self.offset = offset

    def __repr__(self):
        msg = "glymur.jp2box.DataEntryURLBox({0}, {1}, '{2}')"
        msg = msg.format(self.version, self.flag, self.url)
        return msg

    def __str__(self):
        msg = Jp2kBox.__str__(self)
        msg += '\n    '

        lines = ['Version:  {0}',
                 'Flag:  {1} {2} {3}',
                 'URL:  "{4}"']
        msg += '\n    '.join(lines)
        msg = msg.format(self.version,
                         self.flag[0], self.flag[1], self.flag[2],
                         self.url)
        return msg

    @staticmethod
    def parse(fptr, offset, length):
        """Parse data entry URL box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        DataEntryURLbox instance
        """
        read_buffer = fptr.read(4)
        data = struct.unpack('>BBBB', read_buffer)
        version = data[0]
        flag = data[1:4]

        numbytes = offset + length - fptr.tell()
        read_buffer = fptr.read(numbytes)
        url = read_buffer.decode('utf-8')
        box = DataEntryURLBox(version, flag, url, length=length, offset=offset)
        return box


class UUIDBox(Jp2kBox):
    """Container for UUID box information.

    Attributes
    ----------
    box_id : str
        4-character identifier for the box.
    length : int
        length of the box in bytes.
    offset : int
        offset of the box from the start of the file.
    longname : str
        more verbose description of the box.
    uuid : uuid.UUID
        16-byte UUID
    raw_data : byte array
        Sequence of uninterpreted bytes as read from the file.
    data : object
        Specific to each type of UUID.  There are handlers for XMP, Exif, and
        generic (unknown) UUIDs.  In the case of XMP and Exif UUIDs, this is
        the interpreted version of raw_data.

    References
    ----------
    .. [XMP] International Organization for Standardication.  ISO/IEC
       16684-1:2012 - Graphic technology -- Extensible metadata platform (XMP)
       specification -- Part 1:  Data model, serialization and core properties
    """
    def __init__(self, the_uuid, raw_data, length=0, offset=-1):
        """
        Parameters
        ----------
        the_uuid : uuid.UUID
            Identifies the type of UUID box.
        raw_data : byte array
            Sequence of uninterpreted bytes as read from the UUID box.
        length : int
            length of the box in bytes.
        offset : int
            offset of the box from the start of the file.
        """
        Jp2kBox.__init__(self, box_id='uuid', longname='UUID')
        self.uuid = the_uuid
        self.raw_data = raw_data
        self.length = length
        self.offset = offset
        self.data = None

        try:
            self._parse_raw_data()
        except RuntimeError as error:
            warnings.warn(str(error))

    def _parse_raw_data(self):
        """
        Private function for parsing UUID payloads if possible.
        """
        if self.uuid == uuid.UUID('be7acfcb-97a9-42e8-9c71-999491e3afac'):
            self.data = _uuid_io.xml(self.raw_data)
        elif self.uuid.bytes == b'JpgTiffExif->JP2':
            self.data = _uuid_io.tiff_header(self.raw_data)
        else:
            self.data = self.raw_data

    def __repr__(self):
        msg = "glymur.jp2box.UUIDBox(the_uuid={0}, "
        msg += "raw_data=<byte array {1} elements>)"
        return msg.format(repr(self.uuid), len(self.data))

    def __str__(self):
        msg = '{0}\n    UUID:  {1}'.format(Jp2kBox.__str__(self), self.uuid)

        if self.uuid == uuid.UUID('be7acfcb-97a9-42e8-9c71-999491e3afac'):
            line = ' (XMP)\n    UUID Data:  {0}'
            msg += line.format(_pretty_print_xml(self.data))
        elif self.uuid.bytes == b'JpgTiffExif->JP2':
            msg += ' (EXIF)\n    UUID Data:  {0}'.format(str(self.data))
        else:
            line = ' (unknown)\n    UUID Data:  {0} bytes'
            msg += line.format(len(self.raw_data))

        return msg

    def write(self, fptr):
        """Write a UUID box to file.
        """
        if self.uuid != uuid.UUID('be7acfcb-97a9-42e8-9c71-999491e3afac'):
            msg = "Only XMP UUID boxes can currently be written."
            raise NotImplementedError(msg)
        write_buffer = struct.pack('>I4s', self.length, b'uuid')
        fptr.write(write_buffer)
        fptr.write(self.uuid.bytes)
        fptr.write(self.raw_data)

    @staticmethod
    def parse(fptr, offset, length):
        """Parse UUID box.

        Parameters
        ----------
        fptr : file
            Open file object.
        offset : int
            Start position of box in bytes.
        length : int
            Length of the box in bytes.

        Returns
        -------
        UUIDBox instance
        """

        read_buffer = fptr.read(16)
        the_uuid = uuid.UUID(bytes=read_buffer)

        numbytes = offset + length - fptr.tell()
        read_buffer = fptr.read(numbytes)
        box = UUIDBox(the_uuid, read_buffer, length=length, offset=offset)
        return box


# Map each box ID to the corresponding class.
_BOX_WITH_ID = {
    'asoc': AssociationBox,
    'cdef': ChannelDefinitionBox,
    'cmap': ComponentMappingBox,
    'colr': ColourSpecificationBox,
    'ftyp': FileTypeBox,
    'ihdr': ImageHeaderBox,
    'jP  ': JPEG2000SignatureBox,
    'jpch': CodestreamHeaderBox,
    'jplh': CompositingLayerHeaderBox,
    'jp2c': ContiguousCodestreamBox,
    'jp2h': JP2HeaderBox,
    'lbl ': LabelBox,
    'pclr': PaletteBox,
    'res ': ResolutionBox,
    'resc': CaptureResolutionBox,
    'resd': DisplayResolutionBox,
    'rreq': ReaderRequirementsBox,
    'uinf': UUIDInfoBox,
    'ulst': UUIDListBox,
    'url ': DataEntryURLBox,
    'uuid': UUIDBox,
    'xml ': XMLBox}
