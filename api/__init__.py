"""FastAPI service wrapping the property scanner."""

# Loaded here, before `api.config` or `scanner.config` read os.environ at import
# time. On Railway there is no .env file and this is a no-op.
from dotenv import load_dotenv

load_dotenv()
