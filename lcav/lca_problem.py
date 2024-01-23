from typing import Dict, List
import warnings
from pathlib import Path
import dill
import lca_algebraic as lcalg
from lca_algebraic.helpers import Activity
import pandas as pd
from sympy.parsing.sympy_parser import parse_expr


USER_DB = 'Foreground DB'


class LCAProblem:
    """
    LCA Problem. Contains the LCA model, the set of impact methods and the parameters values.
    """

    def __init__(self, load_from_file=None):
        self.project = None  # Project name
        self.model = None  # LCA model
        self.methods = None  # LCIA methods
        self.lambdas = None  # Compiled expressions of impacts
        self.path_to_db = None  # path to background database (with lca algebraic parameters)

        if load_from_file is not None:
            self._load(load_from_file)

    def set_methods(self, methods):
        """
        Sets the LCIA methods for the problem.
        """
        if self.methods is not None:
            warnings.warn("Replacing existing LCIA methods from LCA problem.")
        self.methods = methods

    def compute_lcia(self, parameters:Dict, extract_activities: List[Activity] = None):
        """
        Computes the LCIA using the main LCA_algebraic function.
        First, symbolic expressions of the model for each impact are compiled.
        Then, the expressions are evaluated for the parameter values provided. 

        :param parameters: dict of {parameters: value or list of values}
        :param extract_activities: Optionnal : list of foregound or background activities. If provided, the result only integrate their contribution.

        :return res: dataframe of calculated impacts for each method.
        """

        # Sub processes to consider for the LCIA (useful to calculate individual contributions)
        #if not isinstance(extract_activities, list):
        #    extract_activities = [extract_activities]
        if extract_activities == [self.model]:
            extract_activities = None

        print(self.model, self.methods, extract_activities, parameters)

        # LCIA calculation
        res = lcalg.multiLCAAlgebric(
            self.model,  # The model
            self.methods,  # Impact categories / methods

            # List of sub activities to consider
            extract_activities=extract_activities,

            # Parameters of the model
            **parameters
        )
        return res

    def compile_lcia_functions(self, extract_activities: List[Activity] = None):
        """
        Computes symbolic expressions of the model for each impact assessment method as a function of background activities and parameters.

        :param extract_activities: Optionnal : list of foregound or background activities. If provided, the result only integrate their contribution.
        """

        # Sub processes to consider for the LCIA (useful to calculate individual contributions)
        #if not isinstance(extract_activities, list):
        #    extract_activities = [extract_activities]
        if extract_activities == [self.model]:
            extract_activities = None

        with lcalg.params.DbContext(USER_DB):
            self.lambdas = lcalg.lca._preMultiLCAAlgebric(self.model, self.methods, extract_activities=extract_activities)

    def get_lcia_functions(self):
        """
        Gets the symbolic expressions if the model for each impact method.
        """
        if self.lambdas is None:
            warnings.warn("No compiled expression. Calling `compile_lcia_functions()` to get LCIA functions...")
            self.compile_lcia_functions()
        exprs = {method_name: self.lambdas[i] for i, method_name in enumerate(self.methods)}
        return exprs

    def save(self, file_path: str):
        """
        Saves the LCA problem for future use.
        """
        if not self.lambdas:
            warnings.warn(
                "No symbolic expression registered in LCA problem. Consider calling `compile_lcia_functions()` first.")
        if not file_path.endswith('.pickle'):
            file_path = file_path + '.pickle'
        self.path_to_db = Path(file_path).stem + '.bw2'
        lcalg.export_db(USER_DB, self.path_to_db)
        dill.dump(self.__dict__, file = open(file_path, 'wb'))
        print(f"LCA problem saved in {file_path} and {self.path_to_db}.") 

    def _load(self, file_path: str):
        """
        Loads a pre-existing LCA problem.
        """
        if not file_path.endswith('.pickle'):
            file_path = file_path + '.pickle'
        with open(file_path, 'rb') as file:
            data = dill.load(file)
            self.__dict__.update(data)

        # Setup project
        lcalg.initProject(self.project)

        # Reimport database
        lcalg.import_db(self.path_to_db)
        
        # Cleaning up the whole foreground database would remove the processes declared previously...
        #lcalg.resetDb(USER_DB)
        #lcalg.setForeground(USER_DB)

        # Load parameters
        #lcalg.params.loadParams()
        
        print(f"Loaded LCA problem from {file_path} and {self.path_to_db}.")

    def evaluate_lcia_functions(self, parameters: Dict):
        """
        Evaluates the LCIA functions for a set of parameter values. Requires `compile_lcia_functions` to be called first to get the LCIA expressions.

        :param parameters: dict of {parameters: value or list of values}

        :return res: dataframe of calculated impacts for each method.
        """
        if not self.lambdas:
            warnings.warn("No compiled expression. Calling `compile_lcia_functions()` to get LCIA functions...")
            self.compile_lcia_functions()

        with lcalg.params.DbContext(USER_DB):
            df = lcalg.lca._postMultiLCAAlgebric(self.methods, self.lambdas, **parameters)
            
        return df

    def list_processes(self):
        """
        Traverses the tree of sub-activities (sub-processes) until background database is reached.
        """
        activities = []
        units = []
        locations = []
        parents = []
        exchanges = []
        levels = []
        dbs = []

        def _recursive_activities(act,
                                  activities, units, locations, parents, exchanges, levels, dbs,
                                  parent: str = "", exc: dict = None, level: int = 0):

            if exc is None:
                exc = {}
            name = act.as_dict()['name']
            unit = act.as_dict()['unit']
            loc = act.as_dict()['location']
            if loc != 'GLO':
                name += f' [{loc}]'
            exchange = _getAmountOrFormula(exc)
            db = act.as_dict()['database']


            # to stop BEFORE reaching the first level of background activities
            # if db != USER_DB:  # to stop BEFORE reaching the first level of background activities
            #    return

            activities.append(name)
            units.append(unit)
            locations.append(loc)
            parents.append(parent)
            exchanges.append(exchange)
            levels.append(level)
            dbs.append(db)

            # to stop AFTER reaching the first level of background activities
            if db != USER_DB:
                return

            for exc in act.technosphere():
                _recursive_activities(exc.input, activities, units, locations, parents, exchanges, levels, dbs,
                                      parent=name,
                                      exc=exc,
                                      level=level + 1)
            return

        def _getAmountOrFormula(ex):
            """ Return either a fixed float value or an expression for the amount of this exchange"""
            if 'formula' in ex:
                return parse_expr(ex['formula'])
            elif 'amount' in ex:
                return ex['amount']
            return ""

        _recursive_activities(self.model, activities, units, locations, parents, exchanges, levels, dbs)
        data = {'activity': activities,
                'unit': units,
                'location': locations,
                'level': levels,
                'database': dbs,
                'parent': parents,
                'exchange': exchanges}
        df = pd.DataFrame(data, index=activities)

        # df['description'] = df['activity'] + '\n' + df['unit'].fillna('')

        return df

    @staticmethod
    def list_parameters():
        """
        Lists the parameters defined in the LCA problem.
        """
        parameters = lcalg.list_parameters()
        return parameters

    @staticmethod
    def list_databases():
        """
        Lists the databases used in the LCA problem.
        """
        databases = lcalg.list_databases()
        return databases