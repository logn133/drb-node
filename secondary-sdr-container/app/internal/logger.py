import logging
from logging.handlers import RotatingFileHandler


def create_logger(name, level=logging.DEBUG, max_bytes=10485760, backup_count=2):
    debug_log_file = "./secondary-sdr.debug.log"
    info_log_file = "./secondary-sdr.log"

    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.hasHandlers():
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)

        debug_file_handler = RotatingFileHandler(debug_log_file, maxBytes=max_bytes, backupCount=backup_count)
        debug_file_handler.setLevel(logging.DEBUG)

        info_file_handler = RotatingFileHandler(info_log_file, maxBytes=max_bytes, backupCount=backup_count)
        info_file_handler.setLevel(logging.INFO)

        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(formatter)
        debug_file_handler.setFormatter(formatter)
        info_file_handler.setFormatter(formatter)

        logger.addHandler(console_handler)
        logger.addHandler(debug_file_handler)
        logger.addHandler(info_file_handler)

    return logger
