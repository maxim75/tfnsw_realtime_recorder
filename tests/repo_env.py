"""An env with a dummy value for every ``${VAR}`` the repo's cities.toml uses, so every city loads enabled."""
import re

from recorder.cities import DEFAULT_CITIES_FILE

REPO_ENV = {name: "secret" for name in re.findall(r"\$\{(\w+)\}", DEFAULT_CITIES_FILE.read_text())}
