import logging

logger = logging.getLogger("biometriclink")
logger.setLevel(logging.INFO)
logger.propagate = False  # own handler below; don't duplicate via the root logger
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)
