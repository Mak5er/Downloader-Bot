import datetime
import logging

import pytz

my_timezone = pytz.timezone('Europe/Kyiv')


class CustomFormatter(logging.Formatter):
    def __init__(self, fmt):
        super().__init__(fmt)

    def formatTime(self, record, datefmt=None):
        local_time = datetime.datetime.now(my_timezone)
        return local_time.strftime('%Y-%m-%d %H:%M:%S')


log_format = '%(asctime)s - %(levelname)s - %(message)s'

logging.basicConfig(filename='log/bot_log.log',
                    level=logging.INFO,
                    format=log_format)

custom_formatter = CustomFormatter(log_format)
logging.root.handlers[0].setFormatter(custom_formatter)