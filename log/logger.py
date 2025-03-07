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

# Основний формат логування
log_format = '%(asctime)s - %(levelname)s - %(message)s'

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# Кольоровий обробник для консолі
console_handler = logging.StreamHandler()
color_formatter = colorlog.ColoredFormatter(
    '%(log_color)s%(asctime)s - %(levelname)s - %(message)s%(reset)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_handler.setFormatter(color_formatter)

# Обробник для помилок (Error logs)
error_handler = logging.FileHandler('log/error_log.log')
error_formatter = CustomFormatter('%(asctime)s - %(levelname)s - %(message)s')
error_handler.setFormatter(error_formatter)
error_handler.setLevel(logging.ERROR)

info_handler = logging.FileHandler('log/bot_log.log')
info_formatter = CustomFormatter('%(asctime)s - %(levelname)s - %(message)s')
info_handler.setFormatter(info_formatter)
info_handler.setLevel(logging.INFO)

class MaxLevelFilter(logging.Filter):
    def __init__(self, max_level):
        super().__init__()
        self.max_level = max_level

    def filter(self, record):
        return record.levelno < self.max_level

info_handler.addFilter(MaxLevelFilter(logging.ERROR))

logger.addHandler(console_handler)
logger.addHandler(error_handler)
logger.addHandler(info_handler)

logger.info("This is an info message")
logger.warning("This is a warning message")
logger.error("This is an error message")
logger.critical("This is a critical message")