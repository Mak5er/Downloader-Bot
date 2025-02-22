import datetime
import logging
import pytz
import colorlog

my_timezone = pytz.timezone('Europe/Kyiv')


class CustomFormatter(logging.Formatter):
    def __init__(self, fmt):
        super().__init__(fmt)

    def formatTime(self, record, datefmt=None):
        local_time = datetime.datetime.now(my_timezone)
        return local_time.strftime('%Y-%m-%d %H:%M:%S')


log_format = '%(asctime)s - %(levelname)s - %(message)s'

logger = logging.getLogger()
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
color_formatter = colorlog.ColoredFormatter(
    '%(log_color)s%(asctime)s - %(levelname)s - %(message)s%(reset)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_handler.setFormatter(color_formatter)

file_handler = logging.FileHandler('log/bot_log.log')
custom_formatter = CustomFormatter(log_format)
file_handler.setFormatter(custom_formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

logger.debug("This is a debug message")
logger.info("This is an info message")
logger.warning("This is a warning message")
logger.error("This is an error message")
logger.critical("This is a critical message")