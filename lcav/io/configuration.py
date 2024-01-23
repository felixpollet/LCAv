import json
from importlib.resources import open_text
from abc import ABC, abstractmethod
from jsonschema import validate
import lca_algebraic as lcalg
from sympy import sympify
import logging
import os.path as pth
from ruamel.yaml import YAML

from lcav.io import resources
from lcav.lca_problem import LCAProblem

_LOGGER = logging.getLogger(__name__)

JSON_SCHEMA_NAME = "configuration.json"
USER_DB = 'Foreground DB'
KEY_PROJECT = 'project'
KEY_DATABASE = 'database'
KEY_MODEL = 'model'
KEY_NORMALIZED_MODEL = 'normalized model'
KEY_EXCHANGE = 'exchange'
KEY_EXCHANGES = 'exchanges'
KEY_SWITCH = 'is_switch'
KEY_FUNCTIONAL_VALUE = 'functional_unit_scaler'
KEY_ID = 'id'
KEY_UNIT = 'unit'
EXCHANGE_DEFAULT_VALUE = 1.0  # default value for the exchange between two processes
SWITCH_DEFAULT_VALUE = False  # default foreground process type (True: it is a switch process; False: it is a regular process)
KEY_METHODS = 'methods'


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

    def get_problem(self) -> LCAProblem:
        """
        Builds the LCA problem from current configuration.
        """

        # Create new instance of LCA problem
        problem = LCAProblem()

        # Create model from configuration file
        self._build_model(problem)

        # Add LCIA methods if declared
        methods = self._serializer.data.get(KEY_METHODS)
        if methods:
            problem.methods = self._get_methods()
                
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

    def _setup_project(self, problem):
        """
        Sets the brightway2 project and import the databases 
        """
        if self._serializer.data is None:
            raise RuntimeError("read configuration file first")

        ### Init the brightway2 project
        project_name = problem.project = self._serializer.data.get(KEY_PROJECT)
        lcalg.initProject(project_name)
        
        # Import Ecoinvent DB (if not already done)
        # Update the name and path to the location of the ecoinvent database
        db_dict = self._serializer.data.get(KEY_DATABASE)
        db_name = db_dict['name']
        db_path = db_dict['path']
        lcalg.importDb(db_name, db_path)

        # This is better to cleanup the whole foreground model each time, and redefine it
        # instead of relying on a state or previous run.
        # Any persistent state is prone to errors.
        lcalg.resetDb(USER_DB)
        lcalg.setForeground(USER_DB)
        
        # Parameters are stored at project level : 
        # Reset them also
        # You may remove this line if you import a project and parameters from an external source (see loadParam(..))
        lcalg.resetParams()

    def _build_model(self, problem):
        """
        Builds the LCA model as defined in the configuration file.
        """

        ### Set up the project
        self._setup_project(problem)

        # Retrieve background database
        background_db = self._serializer.data.get(KEY_DATABASE)['name']

        # Get model definition from configuration file
        model_definition = self._serializer.data.get(KEY_MODEL)
        model = lcalg.newActivity(
            db_name=USER_DB, 
            name=KEY_MODEL, 
            unit=None
        )
        
        if KEY_ID in model_definition:
            # The defined model is only one background process
            identifier = model_definition[KEY_ID]
            sub_process = lcalg.findActivity(
                db_name=background_db, 
                code=identifier
            )
            model.addExchanges({sub_process: EXCHANGE_DEFAULT_VALUE})
        else:
            # The defined model is a group
            self._parse_problem_table(model, model_definition)

        # Functional unit scaling
        if KEY_FUNCTIONAL_VALUE in model_definition and model_definition[KEY_FUNCTIONAL_VALUE] != 1.0:
            # create a normalized model whose exchange with the model is equal to the functional value
            model_definition[KEY_EXCHANGE] = model_definition[KEY_FUNCTIONAL_VALUE]
            functional_value = self._parse_exchange(model_definition)
            normalized_model = lcalg.newActivity(
                db_name=USER_DB, 
                name=KEY_NORMALIZED_MODEL, 
                unit=None,
                exchanges = {model : functional_value}
            )
            problem.model = normalized_model

        else:
            problem.model = model
    
    def _parse_problem_table(self, group, table: dict, group_switch_param=None):
        """
        Feeds provided group, using definition in provided table.

        :param group:
        :param table:
        """
        background_db = self._serializer.data.get(KEY_DATABASE)['name']

        for key, value in table.items():
            if isinstance(value, dict):  # value defines a subprocess
                if KEY_ID in value:
                    # It is a background process, that should be registered with its ID
                    identifier = value[KEY_ID]
                    exchange = self._parse_exchange(value)
                    sub_process = lcalg.findActivity(
                        db_name=background_db, 
                        code=identifier
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
                    exchange = self._parse_exchange(value)  # exchange with parent group
                    unit = value[KEY_UNIT] if KEY_UNIT in value else None  # process unit
                    switch_param = None
                    if KEY_SWITCH not in value or (KEY_SWITCH in value and not value[KEY_SWITCH]):
                        # It is a regular process
                        sub_process = lcalg.newActivity(
                            db_name=USER_DB, 
                            name=key, 
                            unit=unit
                        )
                    else:
                        # It is a switch process
                        switch_values = value.copy()
                        switch_values.pop(KEY_EXCHANGE, None)
                        switch_values.pop(KEY_SWITCH)
                        switch_values = list(switch_values.keys())  # [val + '_enum' for val in list(switch_values.keys())]
                        switch_values = [val.replace('_', '') for val in switch_values]  # trick since lca algebraic does not handles correctly switch values with underscores
                        switch_param = lcalg.newEnumParam(
                            name=key+'_switch_param',  # name of switch parameter
                            values=switch_values,  # possible values
                            default=switch_values[0],  # default value
                            dbname=USER_DB  # parameter defined in foreground database
                        )
                        sub_process = lcalg.newSwitchAct(
                            dbname=USER_DB, 
                            name=key, 
                            paramDef=switch_param,
                            acts_dict={}
                        )

                    # Check if parent group is a switch process or a regular process
                    if group_switch_param:
                        # Parent group is a switch process
                        switch_value = key.replace('_', '')  # trick since lca algebraic does not handles correctly switch values with underscores
                        group.addExchanges({sub_process: exchange * group_switch_param.symbol(switch_value)})
                    else:
                        # Parent group is a regular process
                        group.addExchanges({sub_process: exchange})
                    self._parse_problem_table(sub_process, value, switch_param)
    
    def _parse_exchange(self, table: dict):
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
            lcalg.newFloatParam(
                str(param),  # name of the parameter
                default=1.0,  # default value
                min=0.0,
                dbname=USER_DB  # we define the parameter in the foreground database
            )
        
        return expr

    def _get_methods(self):
        """
        Gets the LCIA impact methods defined in the configuration file.
        """
        methods = [eval(m) for m in self._serializer.data.get(KEY_METHODS)]
        return methods

    
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