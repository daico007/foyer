import collections
import glob
import itertools
import os
from tempfile import NamedTemporaryFile
import xml.etree.ElementTree as ET
from lxml import etree
from pkg_resources import resource_filename
import warnings
import re

import numpy as np
import foyer.element as custom_elem

import gmso
#from gmso.external.convert_foyer import from_foyer
from gmso.external import from_mbuild

import simtk.unit as u
from simtk import openmm as mm
from simtk.openmm import app
from simtk.openmm.app.forcefield import (NoCutoff, CutoffNonPeriodic, HBonds,
                                         AllBonds, HAngles, NonbondedGenerator,
                                         _convertParameterToNumber)

from foyer.atomtyper import find_atomtypes
from foyer.exceptions import FoyerError
from foyer import smarts
from foyer.validator import Validator
from foyer.xml_writer import write_foyer
from foyer.utils.io import import_, has_mbuild
from foyer.utils.external import get_ref


# Copy from original forcefield.py
def preprocess_forcefield_files(forcefield_files=None, backend='gmso'):
    """Pre-process foyer Forcefield XML files"""
    if forcefield_files is None:
        return None

    preprocessed_files = []

    for xml_file in forcefield_files:
        if not hasattr(xml_file, 'read'):
            f = open(xml_file)
            _, suffix = os.path.split(xml_file)
        else:
            f = xml_file
            suffix = ""

        # read and preprocess
        xml_contents = f.read()
        f.close()
        xml_contents = re.sub(r"(def\w*=\w*[\"\'])(.*)([\"\'])", lambda m: m.group(1) + re.sub(r"&(?!amp;)", r"&amp;", m.group(2)) + m.group(3),
                              xml_contents)

        try:
            '''
            Sort topology objects by precedence, defined by the number of
            `type` attributes specified, where a `type` attribute indicates
            increased specificity as opposed to use of `class`
            '''
            root = ET.fromstring(xml_contents)
            for element in root:
                if 'Force' in element.tag:
                    element[:] = sorted(element, key=lambda child: (
                        -1 * len([attr_name for attr_name in child.keys()
                                    if 'type' in attr_name])))
            xml_contents = ET.tostring(root, method='xml').decode()
        except ET.ParseError:
            '''
            Provide the user with a warning if sorting could not be performed.
            This indicates a bad XML file, which will be passed on to the
            Validator to yield a more descriptive error message.
            '''
            warnings.warn('Invalid XML detected. Could not auto-sort topology '
                          'objects by precedence.')

        # write to temp file
        temp_file = NamedTemporaryFile(suffix=suffix, delete=False)
        with open(temp_file.name, 'w') as temp_f:
            temp_f.write(xml_contents)

        # append temp file name to list
        preprocessed_files.append(temp_file.name)

    if backend == 'openmm':
        return preprocessed_files
    elif backend == 'gmso':
        # Run through the forcefield XML conversion
        return preprocess_files
    else:
        raise FoyerError('Backend not supported')

class Forcefield(object):
    """General Forcefield object that can be created by either GMSO Forcefield or OpenMM Forcefield

    Parameters
    ----------
    forcefield_files : list of str, optional, default=None
        List of forcefield files to load
    name : str, optional, None
        Name of a forcefield to load that is packaged within foyer
    backend : str, optional, default='openmm'
        Name of the backend used to store all the Types' information.
        Can choose between 'openmm' and 'gmso'

    """
    def __init__(self, forcefield_files=None, name=None,
                       validation=True, backend='gmso',
                       debug=False):
        self.atomTypeDefinitions = dict()
        self.atomTypeOverrides = dict()
        self.atomTypeDesc = dict()
        self.atomTypeRefs = dict()
        self.atomTypeClasses = dict()
        self.atomTypeElements = dict()
        self._included_forcefields = dict()
        self.non_element_types = dict()
        self._version = None
        self._name = None

        if forcefield_files is not None:
            if isinstance(forcefield_files, (list, tuple, set)):
                all_files_to_load = list(forcefield_files)
            else:
                all_files_to_load = [forcefield_files]

        if name is not None:
            try:
                file = self.included_forcefields[name]
            except KeyError:
                raise IOError('Forcefild {} cannot be found.'.format(name))
            else:
                all_files_to_load = [file]

        # Preprocessed the input files
        preprocessed_files = preprocess_forcefield_files(all_files_to_load, backend=backend)
        if validation:
            for ff_file_name in preprocessed_files:
                Validator(ff_file_name, debug)

        # Load in an internal forcefield object depends on given backend
        if backend == 'gmso':
            self._parse_gmso(*preprocessed_files)
        elif backend == 'openmm':
            self._parse_mm(*preprocessed_files)
        elif backend == 'openff':
            self._parse_ff(*preprocessed_files)
        else:
            raise FoyerError("Unsupported backend")

        #Remove the temporary files afterward
            for ff_file_name in preprocessed_files:
                os.remove(ff_file_name)

        self.parser = smarts.SMARTS(self.non_element_types)

    @property
    def version(self):
        return self._version

    @property
    def name(self):
        return self._name

    # Parse forcefield meta information
    def _parse_gmso(self, forcefield_files):
        """ Parse meta fata information when using GMSO as backend
        """
        self.ff = gmso.ForceField(forcefield_files)
        self._version = self.ff.version
        self._name = self.ff.name
        for name, atype in self.ff.atom_types.items():
            self.atomTypeDefinitions[name] = atype.definition
            self.atomTypeOverrides[name] = atype.overrides
            self.atomTypeDesc[name] = atype.description
            self.atomTypeRefs[name] = atype.doi
            self.atomTypeClasses[name] = atype.atomclass
            #self.atomTypeElements[name] = atype.element

    def _parse_mm(self, forcefield_files):
        """ Parse meta data information when using OpenMM as backend
        """
        self.ff = app.ForceField(forcefield_files)
        tree = ET.parse(forcefield_files)
        root = tree.getroot()
        self._version = root.attrib.get('version')
        self._name = root.attrib.get('name')
        
        for atypes_group in root.findall('AtomTypes'):
            for atype in atypes_group:
                name = atype.attrib['name']
                if 'def' in atype.attrib:
                    self.atomTypeDefinitions[name] = atype.attrib['def']
                if 'overrides' in atype.attrib:
                    overrides = set(atype_name.strip() for atype_name in
                                    atype.attrib['overrides'].split(','))
                    if overrides:
                        self.atomTypeOverrides[name] = overrides
                if 'desc' in atype.attrib:
                    self.atomTypeDesc[name] = atype.attrib['desc']
                if 'doi' in atype.attrib:
                    dois = set(doi.strip() for doi in
                               atype.attrib['doi'].split(','))
                    self.atomTypeRefs[name] = dois
                if 'element' in atype.attrib:
                    # Could potentially use ele here instead of just a string
                    self.atomTypeElements[name] = atype.attrib['element']
                if 'class' in atype.attrib:
                    self.atomTypeClasses[name] = atype.attrib['class']
        return None

    def _parse_ff(self, forcefield_files):
        """ Parse meta data information when using OpenFF as backend
        """
        self.ff = app.ForceField(forcefield.files)
        return None
