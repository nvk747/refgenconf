#!/usr/bin/env python

import attmap
import yaml

CONFIG_ENV_VARS = ["REFGENIE"]
CONFIG_NAME = "genome configuration"

class RefGenomeConfiguration(attmap.PathExAttMap):

    def get_index(self, genome_name, index_name):
        if not genome_name in self.genomes:
            msg = "Your genomes do not include {}".format(genome_name)
            raise MissingGenomeError(msg)

        if not index_name in self.genomes[genome_name]:
            msg = "Genome {} exists, but index {} is missing".format(genome_name, index_name)
            raise MissingIndexError(msg)

            return self.genomes[genome_name][index_name]

    def list_genomes(self):
        return list(self.genomes.keys())


    def list_assets(self):
        string = ""
        for genome, values in self.genomes.items():
            string += "  {}: {}\n".format(genome, "; ".join(list(values)))
        return string

    def idx(self):
        retval = {}
        for genome, values in self.genomes.items():
            retval[genome] = list(values)

        return retval

    def to_yaml(self):
        ## TODO: use a recursive dict function for attmap representation
        try:
            return yaml.dump(self.__dict__, default_flow_style=False)
        except yaml.representer.RepresenterError:
            print("SERIALIZED SAMPLE DATA: {}".format(self))
            raise


def load_genome_config(filename):
    return select_load_config(filename, CONFIG_ENV_VARS, CONFIG_NAME)





# These functions should move into 'yacman' package

def load_yaml(filename):
    import yaml
    with open(filename, 'r') as f:
        data = yaml.load(f, yaml.SafeLoader)
    return data


def get_first_env_var(ev):
    """
    Get the name and value of the first set environment variable

    :param ev: a list of the environment variable names
    :type: list[str] | str
    :return: name and the value of the environment variable
    :rtype: list
    """
    if not isinstance(ev, list):
        if isinstance(ev, str):
            ev = [ev]
        else:
            raise TypeError("The argument has to be a list or string.")
    for i in ev:
        if os.getenv(i, False):
            return [i, os.getenv(i)]

def select_load_config(config_filepath=None, 
                        config_env_vars=None, 
                        config_name="config file", 
                        default_config_filepath=None):

    selected_filepath = None

    # First priority: given file
    if config_filepath:
        if not os.path.isfile(config_filepath):
            _LOGGER.error("Config file path isn't a file: {}".
                          format(config_filepath))
            raise IOError(config_filepath)
        else:
            selected_filepath = config_filepath
    else:
        _LOGGER.debug("No local config file was provided")
        # Second priority: environment variables (in priority order)
        if config_env_vars:
            _LOGGER.debug("Checking for environment variable: {}".format(CONFIG_ENV_VARS))

            cfg_env_var, cfg_file = get_first_env_var(self.compute_env_var) or ["", ""]

            if os.path.isfile(cfg_file):
                _LOGGER.debug("Found config file in {}: {}".
                             format(cfg_env_var, cfg_file))
                selected_filepath = cfg_file
            else:
                _LOGGER.info("Using default config file, no global config file provided in environment "
                             "variable(s): {}".format(str(self.compute_env_var)))
                selected_filepath = default_config_filepath
        else:
            _LOGGER.error("No configuration file found.")

    try:
        config_data = load_yaml(selected_filepath)
    except Exception as e:
        _LOGGER.error("Can't load config file '%s'",
                      str(selected_filepath))
        _LOGGER.error(str(type(e).__name__) + str(e))

    return config_data
