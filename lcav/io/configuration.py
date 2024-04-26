import json
import os
from importlib.resources import open_text
from abc import ABC, abstractmethod

import bw2io
from jsonschema import validate
import lca_algebraic as agb
from lca_algebraic.log import warn
import brightway2 as bw
from sympy import sympify
import logging
import os.path as pth
from ruamel.yaml import YAML
from dotenv import load_dotenv

from lcav.io import resources
from lcav.lca_problem import LCAProblem

BIOSPHERE3_DB_NAME="biosphere3"

_LOGGER = logging.getLogger(__name__)

JSON_SCHEMA_NAME = "configuration.json"
USER_DB = 'Foreground DB'
KEY_PROJECT = 'project'
KEY_ECOINVENT = 'ecoinvent'
KEY_MODEL = 'model'
KEY_NORMALIZED_MODEL = 'normalized model'
KEY_EXCHANGE = 'exchange'
KEY_EXCHANGES = 'exchanges'
KEY_SWITCH = 'is_switch'
# KEY_FUNCTIONAL_VALUE = 'functional_unit_scaler'
KEY_NAME = 'name'
KEY_LOCATION = 'loc'
KEY_CATEGORIES = 'categories'
KEY_UNIT = 'unit'
KEY_CUSTOM_ATTR = 'custom_attributes'
KEY_ATTR_NAME = 'attribute'
KEY_ATTR_VALUE = 'value'
EXCHANGE_DEFAULT_VALUE = 1.0  # default value for the exchange between two processes
SWITCH_DEFAULT_VALUE = False  # default foreground process type (True: it is a switch process; False: it is a regular process)
KEY_METHODS = 'methods'


def _get_unique_activity_name(key):
    """
    Returns a unique activity name by incrementing a suffix number.
    """
    i = 1
    while agb.findActivity(key + str(i), db_name=USER_DB, single=False):
        i += 1
    new_key = f'{key}_{i}'
    return new_key


def _parse_exchange(table: dict):
    """
    Gets the exchange expression from the table of options for the process, then creates the input parameters and returns the symbolic expression of the exchange

    :param table: the table of options for the process
    :return expr: the symbolic expression
    """

    # Get the 'exchange' value. If it doesn't exist then set the exchange value to 1
    exchange = table[KEY_EXCHANGE] if KEY_EXCHANGE in table else EXCHANGE_DEFAULT_VALUE

    # Parse the string expression
    expr = sympify(exchange)

    # Get the parameters involved in the expression
    parameters = expr.free_symbols

    # Create the parameters
    for param in parameters:
        agb.newFloatParam(
            str(param),  # name of the parameter
            default=1.0,  # default value
            min=1.0,
            max=1.0,
            dbname=USER_DB  # we define the parameter in the foreground database
        )

    return expr


class LCAProblemConfigurator:
    """
    class for configuring an LCA_algebraic problem from a configuration file

    See :ref:`description of configuration file <configuration-file>`.

    :param conf_file_path: if provided, configuration will be read directly from it
    """
    
    def __init__(self, conf_file_path=None):
        self._conf_file = None

        self._serializer = _YAMLSerializer()

        if conf_file_path:
            self.load(conf_file_path)

    def generate(self):
        """
        Creates the LCA activities and parameters as defined in the configuration file.
        Also gets the LCIA methods defined in the conf file.
        :return: model: the top-level activity corresponding to the functional unit
        :return: methods: the LCIA methods
        """
        # Create model from configuration file
        project_name, model = self._build_model()

        # Get LCIA methods if declared
        methods = [eval(m) for m in self._serializer.data.get(KEY_METHODS, [])]

        return project_name, model, methods

    def get_problem(self) -> LCAProblem:
        """
        Builds an LCA problem from current configuration.
        """

        # Create new instance of LCA problem
        problem = LCAProblem()

        # Generate LCA problem from conf file
        project_name, model, methods = self.generate()

        # Set attributes
        problem.project = project_name
        problem.model = model
        problem.methods = methods
                
        return problem

    def load(self, conf_file):
        """
        Reads the problem definition

        :param conf_file: Path to the file to open or a file descriptor
        """

        self._conf_file = pth.abspath(conf_file)  # for resolving relative paths

        self._serializer = _YAMLSerializer()
        self._serializer.read(self._conf_file)

        # Syntax validation
        with open_text(resources, JSON_SCHEMA_NAME) as json_file:
            json_schema = json.loads(json_file.read())
        
        validate(self._serializer.data, json_schema)
        # Issue a simple warning for unknown keys at root level
        for key in self._serializer.data:
            if key not in json_schema["properties"].keys():
                _LOGGER.warning('Configuration file: "%s" is not a key declared in LCAv.', key)

    def _setup_project(self):
        """
        Sets the brightway2 project and import the databases 
        """
        if self._serializer.data is None:
            raise RuntimeError("read configuration file first")

        ### Init the brightway2 project
        project_name = self._serializer.data.get(KEY_PROJECT)
        bw.projects.set_current(project_name)
        
        ### Import Ecoinvent DB
        ei_dict = self._serializer.data.get(KEY_ECOINVENT)
        ei_version = ei_dict['version']
        ei_model = ei_dict['system_model']

        # This load .env file that contains the credential for EcoInvent into os.environ
        # User must create a file named .env, that he will not share /commit, and contains the following :
        # ECOINVENT_LOGIN=<your_login>
        # ECOINVENT_PASSWORD=<your_password>
        load_dotenv()
        if not os.getenv("ECOINVENT_LOGIN") or not os.getenv("ECOINVENT_PASSWORD"):
            raise RuntimeError("Missing Ecoinvent credentials. Please set them in a .env file in the root of the project. \n"
                               "The file should contain the following lines : \n"
                               "ECOINVENT_LOGIN=<your_login>\n"
                               "ECOINVENT_PASSWORD=<your_password>\n")

        # This downloads ecoinvent and installs biopshere + technosphere + LCIA methods
        if len(bw.databases) > 0:
            print("Initial setup already done, skipping")
        else:
            # This is now the prefered method to init an Brightway2 with Ecoinvent
            # It is not more tied to a specific version of bw2io
            bw2io.import_ecoinvent_release(
                version=ei_version,
                system_model=ei_model,
                username=os.environ["ECOINVENT_LOGIN"],  # Read for .env file
                password=os.environ["ECOINVENT_PASSWORD"],  # Read from .env file
                use_mp=True)

        ### Set the foreground database
        # This is better to cleanup the whole foreground model each time, and redefine it
        # instead of relying on a state or previous run.
        # Any persistent state is prone to errors.
        agb.resetDb(USER_DB)
        agb.setForeground(USER_DB)
        
        # Parameters are stored at project level : 
        # Reset them also
        # You may remove this line if you import a project and parameters from an external source (see loadParam(..))
        agb.resetParams()

        return project_name

    def _build_model(self):
        """
        Builds the LCA model as defined in the configuration file.
        """

        ### Set up the project
        project_name = self._setup_project()

        # Get model definition from configuration file
        model_definition = self._serializer.data.get(KEY_MODEL)
        model = agb.newActivity(
            db_name=USER_DB, 
            name=KEY_MODEL, 
            unit=None
        )
        
        if KEY_NAME in model_definition:
            # The defined model is only one background process
            name = model_definition[KEY_NAME]
            loc = model_definition.get(KEY_LOCATION, None)
            unit = model_definition.get(KEY_UNIT, None)
            categories = model_definition.get(KEY_CATEGORIES, None)
            try:  # Search activity in ecoinvent db
                sub_process = agb.findTechAct(
                    name=name,
                    loc=loc,
                    unit=unit
                )
            except:  # Search activity in biosphere3 db
                sub_process = agb.findBioAct(
                    name=name,
                    loc=loc,
                    categories=categories,
                    unit=unit
                )
            model.addExchanges({sub_process: EXCHANGE_DEFAULT_VALUE})
        else:
            # The defined model is a group
            self._parse_problem_table(model, model_definition)

        # Functional unit scaling
        # if KEY_FUNCTIONAL_VALUE in model_definition and model_definition[KEY_FUNCTIONAL_VALUE] != 1.0:
        #   # create a normalized model whose exchange with the model is equal to the functional value
        #   model_definition[KEY_EXCHANGE] = model_definition[KEY_FUNCTIONAL_VALUE]
        #    functional_value = self._parse_exchange(model_definition)
        #    normalized_model = agb.newActivity(
        #        db_name=USER_DB,
        #        name=KEY_NORMALIZED_MODEL,
        #        unit=None,
        #        exchanges={model: functional_value}
        #    )
        #    problem.model = normalized_model

        return project_name, model
    
    def _parse_problem_table(self, group, table: dict, group_switch_param=None):
        """
        Feeds provided group, using definition in provided table.

        :param group:
        :param table:
        """

        for key, value in table.items():
            if isinstance(value, dict):  # value defines a subprocess
                # Check if an activity with this key as already been defined to avoid overriding it
                if agb.findActivity(key, db_name=USER_DB, single=False):
                    warn(f"Activity with name '{key}' defined multiple times. "
                         f"Adding suffix increments to labels.")
                    key = _get_unique_activity_name(key)

                if KEY_NAME in value:
                    # It is a background process
                    name = value[KEY_NAME]
                    loc = value.get(KEY_LOCATION, None)
                    unit = value.get(KEY_UNIT, None)
                    categories = value.get(KEY_CATEGORIES, None)
                    exchange = _parse_exchange(value)
                    try:  # Search activity in ecoinvent db
                        sub_process = agb.findTechAct(
                            name=name,
                            loc=loc,
                            unit=unit
                        )
                        # Copy activity to foreground database so that we can safely modify it
                        sub_process = agb.copyActivity(
                            USER_DB,
                            sub_process,
                            key
                        )
                        # Fix for mismatch chemical formulas (until fixed by future brightway/lca-algebraic releases)
                        for ex in sub_process.exchanges():
                            if "formula" in ex:
                                del ex["formula"]
                                ex.save()
                        # Add custom attributes
                        for attr in value.get(KEY_CUSTOM_ATTR, []):
                            attr_dict = {attr.get(KEY_ATTR_NAME): attr.get(KEY_ATTR_VALUE)}
                            sub_process.updateMeta(**attr_dict)
                    except:  # Search activity in biosphere3 db
                        sub_process = agb.findBioAct(
                            name=name,
                            loc=loc,
                            categories=categories,
                            unit=unit
                        )

                    if group_switch_param:
                        # Parent group is a switch process
                        switch_value = key.replace('_', '')  # trick since lca algebraic does not handles correctly switch values with underscores
                        group.addExchanges({sub_process: exchange * group_switch_param.symbol(switch_value)})
                    else:
                        # Parent group is a regular process
                        group.addExchanges({sub_process: exchange})
                else:
                    # It is a group
                    exchange = _parse_exchange(value)  # exchange with parent group
                    unit = value.get(KEY_UNIT, None)  # process unit
                    is_switch = value.get(KEY_SWITCH, None)
                    switch_param = None
                    if not is_switch:
                        # It is a regular process
                        sub_process = agb.newActivity(
                            db_name=USER_DB, 
                            name=key, 
                            unit=unit
                        )
                        # Add custom attributes
                        for attr in value.get(KEY_CUSTOM_ATTR, []):
                            attr_dict = {attr.get(KEY_ATTR_NAME): attr.get(KEY_ATTR_VALUE)}
                            sub_process.updateMeta(**attr_dict)
                    else:
                        # It is a switch process
                        switch_values = value.copy()
                        switch_values.pop(KEY_EXCHANGE, None)
                        switch_values.pop(KEY_SWITCH)
                        switch_values = list(switch_values.keys())  # [val + '_enum' for val in list(switch_values.keys())]
                        switch_values = [val.replace('_', '') for val in switch_values]  # trick since lca algebraic does not handles correctly switch values with underscores
                        switch_param = agb.newEnumParam(
                            name=key+'_switch_param',  # name of switch parameter
                            values=switch_values,  # possible values
                            default=switch_values[0],  # default value
                            dbname=USER_DB  # parameter defined in foreground database
                        )
                        sub_process = agb.newSwitchAct(
                            dbname=USER_DB, 
                            name=key, 
                            paramDef=switch_param,
                            acts_dict={}
                        )
                        # Add custom attributes
                        for attr in value.get(KEY_CUSTOM_ATTR, []):
                            attr_dict = {attr.get(KEY_ATTR_NAME): attr.get(KEY_ATTR_VALUE)}
                            sub_process.updateMeta(**attr_dict)

                    # Check if parent group is a switch process or a regular process
                    if group_switch_param:
                        # Parent group is a switch process
                        switch_value = key.replace('_', '')  # trick since lca algebraic does not handles correctly switch values with underscores
                        group.addExchanges({sub_process: exchange * group_switch_param.symbol(switch_value)})
                    else:
                        # Parent group is a regular process
                        group.addExchanges({sub_process: exchange})
                    self._parse_problem_table(sub_process, value, switch_param)


class _IDictSerializer(ABC):
    """Interface for reading and writing dict-like data"""

    @property
    @abstractmethod
    def data(self) -> dict:
        """
        The data that have been read, or will be written.
        """

    @abstractmethod
    def read(self, file_path: str):
        """
        Reads data from provided file.
        :param file_path:
        """

    @abstractmethod
    def write(self, file_path: str):
        """
        Writes data to provided file.
        :param file_path:
        """


class _YAMLSerializer(_IDictSerializer):
    """YAML-format serializer."""

    def __init__(self):
        self._data = None

    @property
    def data(self):
        return self._data

    def read(self, file_path: str):
        yaml = YAML(typ="safe")
        with open(file_path) as yaml_file:
            self._data = yaml.load(yaml_file)

    def write(self, file_path: str):
        yaml = YAML()
        yaml.default_flow_style = False
        with open(file_path, "w") as file:
            yaml.dump(self._data, file)