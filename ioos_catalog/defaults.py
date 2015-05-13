import pkg_resources


CAPTCHA_FONT_PATH = pkg_resources.resource_filename('flask_captcha', 'fonts/Vera.ttf')

from flask_captcha.settings import CAPTCHA_FONT_SIZE, CAPTCHA_LETTER_ROTATION, CAPTCHA_BACKGROUND_COLOR, CAPTCHA_FOREGROUND_COLOR
from flask_captcha.settings import CAPTCHA_CHALLENGE_FUNCT, CAPTCHA_WORDS_DICTIONARY, CAPTCHA_PUNCTUATION, CAPTCHA_FLITE_PATH
from flask_captcha.settings import CAPTCHA_TIMEOUT, CAPTCHA_LENGTH, CAPTCHA_IMAGE_BEFORE_FIELD, CAPTCHA_DICTIONARY_MIN_LENGTH
from flask_captcha.settings import CAPTCHA_DICTIONARY_MAX_LENGTH, CAPTCHA_OUTPUT_FORMAT, CAPTCHA_NOISE_FUNCTIONS, CAPTCHA_FILTER_FUNCTIONS
CAPTCHA_PREGEN = False
