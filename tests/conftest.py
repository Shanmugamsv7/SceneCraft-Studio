"""Keep the test process isolated from ignored local credentials in .env."""

import os


os.environ["SCENECRAFT_DISABLE_DOTENV"] = "1"
