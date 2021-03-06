VERSION = "1.1"

import os, sys, time, traceback, json, re, logging, logging.handlers
from datetime import timedelta, datetime
import requests, jsonschema
from notify import Emailer

class Validator(object):
    """Represents a Validator as stored in the json settings file"""
    def __init__(self, endpoint, username, password):
        super(Validator, self).__init__()
        self.endpoint = endpoint
        self.username = username
        self.password = password

    def start(self, feed):
        payload = {
            "url": feed.endpoint,
            "username": feed.username,
            "password": feed.password,
            "which-checks": "read-only",
            "validation-type": "all-data",
        }

        try:
            # We just assume this is going to timeout and move to polling the results endpoint
            requests.get(self.endpoint, auth = (self.username, self.password), params = payload, timeout = 1)
        except requests.exceptions.Timeout:
            feed.validation_start_time = datetime.now()

    def poll_results(self, feed):
        validation_finished = False
        total_issues = 0
        response_json = None
        payload = {
            "validation-type": "all-data",
            "results": feed.endpoint,
            "json": 1,
        }

        response = requests.get(self.endpoint, auth = (self.username, self.password), params = payload, timeout = 10)
        if response.status_code == 200:
            validation_finished, total_issues, response_json = self.handle_results_response(feed, response.text)

        return validation_finished, total_issues == 0, total_issues, response_json

    def handle_results_response(self, feed, response):
        validation_finished = False
        total_issues = 0
        response_json = json.loads(response)

        with open("{0}/schemas/results.json".format(os.path.dirname(os.path.realpath(__file__))), "r") as schema_file:
            jsonschema.validate(response_json, json.load(schema_file))

        if datetime.fromtimestamp(response_json['test-time']) > feed.validation_start_time:
            validation_finished = True
            feed.last_validated = datetime.now()
            feed.validation_start_time = None

            if feed.ignore_warnings:
                total_issues = len(response_json['validation-results']['errors']) if 'errors' in response_json['validation-results'] else 0
            else:
                total_issues = int(response_json['total-issue-count'])

        return validation_finished, total_issues, response_json

class Feed(object):
    """Represents a Feed as stored in the json settings file"""
    def __init__(self, name, endpoint, username, password, next_try, ignore_warnings, failure_email):
        super(Feed, self).__init__()
        self.last_validated = None
        self.validation_start_time = None
        self.name = name
        self.endpoint = endpoint
        self.username = username
        self.password = password
        self.failure_email = failure_email
        self.ignore_warnings = ignore_warnings

        result = re.match('^(\d+)([m|M|h|H|d|D])$', next_try)
        if result:
            duration = int(result.group(1))
            period = result.group(2).lower()

            if duration == 0:
                raise ValueError('Invalid next_try value provided. Valid format is [delta][m|h|d] (i.e "10m", "3h", "2d")')

            duration_types = {"m": "minutes", "h": "hours", "d": "days"}
            delta_kwargs = {}
            delta_kwargs[duration_types[period]] = duration

            self.next_try = timedelta(**delta_kwargs)
        else:
            raise ValueError('Invalid next_try value provided. Valid format is [delta][m|h|d] (i.e "10m", "3h", "2d")')

class JsonSettings(object):
    def __init__(self, json_path):
        super(JsonSettings, self).__init__()
        self.json_data = self.load(json_path)
        self.validate()

    def validate(self):
        with open("{0}/schemas/settings.json".format(os.path.dirname(os.path.realpath(__file__))), "r") as schema_file:
            jsonschema.validate(self.json_data, json.load(schema_file))

    def load(self, json_path):
        try:
            json_file = open(json_path)
        except IOError:
            raise IOError('The specified json settings file does not exist: {0}'.format(json_path))
        json_data = json.load(json_file)
        json_file.close()

        return json_data

def main():
    # Deal with the command line arguments
    current_dir = os.path.dirname(os.path.realpath(__file__))
    settings_path = sys.argv[1] if len(sys.argv) >= 2 else '{0}/settings.json'.format(current_dir)
    log_path = sys.argv[2] if len(sys.argv) >= 3 else '{0}/flmx-validator.log'.format(current_dir)

    # First off set up the logging.
    logger = logging.getLogger('flmx-logger')
    logger.setLevel(logging.DEBUG)

    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=104857600, backupCount=3)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    try:
        # Load json settings, either from command line argument or default location.
        settings = JsonSettings(settings_path)
        logger.info("Settings loaded from {0}".format(settings_path))

        # Setup validator, emailer and feeds.
        validator = Validator(**settings.json_data['validator'])
        logger.info("Validator at endpoint {0} initialised".format(validator.endpoint))

        feeds = []
        for feed in settings.json_data['feeds']:
            f = Feed(**feed)
            feeds.append(f)
            logger.info("Feed at endpoint {0} initialised".format(f.endpoint))

        emailer = Emailer(settings.json_data['email'])

        # Start validation loop.
        while (True):
            for feed in feeds:
                # If feed is not currently being validated, and it was last validated longer than [next_try] ago, start validation.
                if feed.validation_start_time is None and (feed.last_validated is None or datetime.now() > feed.last_validated + feed.next_try):
                    logger.info("Sending validation request for {0} [{1}] to {2}".format(feed.name, feed.endpoint, validator.endpoint))
                    validator.start(feed)

                # Else if the validation must have started
                elif feed.validation_start_time is not None:
                    logger.info("Polling validation results for {0} [{1}] from {2}".format(feed.name, feed.endpoint, validator.endpoint))
                    completed, success, total_issues, response_json = validator.poll_results(feed)

                    # If the process has completed and the result was a failure, send an email notification.
                    if completed:
                        if not success:
                            logger.info("Validation for {0} [{1}] resulted in errors, sending email to {2}".format(feed.name, feed.endpoint, feed.failure_email))

                            title = u'Validation failed for {feed} [{endpoint}] ({total_issues} {issues})'.format(
                                    feed = feed.name,
                                    endpoint = feed.endpoint,
                                    total_issues = total_issues,
                                    issues = "Issues" if total_issues > 1 else "Issue")
                            body = json.dumps(response_json, indent=4, separators=(',', ': '), sort_keys=True)
                            emailer.send(feed.failure_email, title, body)

                            logger.info("Email sent to {0}".format(feed.failure_email))
                        else:
                            logger.info("Validation completed successfully for {0} [{1}]".format(feed.name, feed.endpoint))

                    # Check to make sure we haven't hit some weird behaviour and have been stuck polling for > 6 hours.
                    elif feed.last_validated is not None and datetime.now() > feed.last_validated + timedelta(hours=6):
                        # If we have then let's just kick off another validation request.
                        feed.validation_start_time = None
                        logger.debug("Have been polling {0} for > 6 hours, must be a problem lets rinse and repeat.".format(feed.name))
                        break

            time.sleep(300) # Wait for a bit to try again, hopefully this should account for most differences in time between the executing and validation server too.

    except Exception as e:
        logger.debug("Unhandled exception occured: {0}".format(e))
        logger.debug(traceback.format_exc())
        logger.debug("Closing application.")

        sys.exit()

if __name__ == '__main__':
    main()