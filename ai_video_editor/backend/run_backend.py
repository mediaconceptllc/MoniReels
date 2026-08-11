"""PyInstaller entry point. Lives outside the `app` package so `from app.x
import y` absolute imports keep working once frozen (a frozen entry point
living *inside* app/ would put app/ itself on sys.path instead of backend/).
"""
from app.main import main

if __name__ == "__main__":
    main()
