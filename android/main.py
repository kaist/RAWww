"""Android entry point.

``pyside6-android-deploy`` requires the main entry point to be named
``main.py``. It forces the mobile build profile and launches the trimmed
ShotSync selection shell (no AI / XMP / utilities / RAW / filesystem browser).
"""

import os

os.environ.setdefault("RAWWW_PROFILE", "mobile")

from rawww.mobile import main

if __name__ == "__main__":
    main()
