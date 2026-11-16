"""نقطة تشغيل برنامج مطابقة حسابات الصرافين."""
import sys

from sarraf_recon.ui.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
