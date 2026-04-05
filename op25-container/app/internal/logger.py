import logging
from logging.handlers import RotatingFileHandler

def create_logger(name, level=logging.DEBUG, max_bytes=10485760, backup_count=2):
    """
    Creates a logger with a console and rotating file handlers for both debug and info log levels.

    Args:
        name (str): The name for the logger.
        level (int): The logging level for the logger. Defaults to logging.DEBUG.
        max_bytes (int): Maximum size of the log file in bytes before it gets rotated. Defaults to 10 MB.
        backup_count (int): Number of backup files to keep. Defaults to 2.

    Returns:
        logging.Logger: Configured logger.
    """
    # Set the log file paths
    debug_log_file = "./client.debug.log"
    info_log_file = "./client.log"

    # Create a logger
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Check if the logger already has handlers to avoid duplicate logs
    if not logger.hasHandlers():
        # Create console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)

        # Create rotating file handler for debug level
        debug_file_handler = RotatingFileHandler(debug_log_file, maxBytes=max_bytes, backupCount=backup_count)
        debug_file_handler.setLevel(logging.DEBUG)

        # Create rotating file handler for info level
        info_file_handler = RotatingFileHandler(info_log_file, maxBytes=max_bytes, backupCount=backup_count)
        info_file_handler.setLevel(logging.INFO)

        # Create formatter and add it to the handlers
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(formatter)
        debug_file_handler.setFormatter(formatter)
        info_file_handler.setFormatter(formatter)

        # Add the handlers to the logger
        logger.addHandler(console_handler)
        logger.addHandler(debug_file_handler)
        logger.addHandler(info_file_handler)

    return logger

# Example usage:
# logger = create_logger('my_logger')
# logger.debug('This is a debug message')
# logger.info('This is an info message')
