#!/usr/bin/env python

"""
Web Service Simulator

@author: Matthew Kennard <matthew.kennard@oneresult.co.uk>
"""

import BaseHTTPServer
import sys
import os
import time
import random
import re
import types
from urlparse import urlparse, parse_qs
from optparse import OptionParser
import ConfigParser
import string
import subprocess
import tempfile
import traceback
import StringIO
import code

from fsevents import Observer
from fsevents import Stream


# Address the server will listen on
IP_ADDRESS = '127.0.0.1'
# Port the server will listen on (80 will require being run as root)
PORT = 8000

call_handler = None
httpd = None


class ParameterMatch(object):
    """
    Corresponds to a v_<key> = [!~]<value> in the server

    Will see whether a particular key and value combination
    matches this defined parameter. A parameter can be an
    inverse_match (matches if the value being tested is
    not a particular value), and can be an optional value
    so if not specified then matches
    """

    def __init__(self, key, value, inverse_match=False, optional=False):
        """
        @param key: The key
        @param value: The value which will be a match
        @param inverse_match: Whether should match if NOT value
        @param optional: Whether this is an optional parameter
        """
        self.key = key
        self.value = value
        self.inverse_match = inverse_match
        self.optional = optional

    def match(self, possible_key, possible_value):
        """
        @return: True if possible_value matches self.value (unless of course
        inverse_match or optional is set)
        """
        if self.key == possible_key:
            if possible_value is None and self.optional:
                return True
            if self.value == possible_value and not self.inverse_match:
                return True
            if self.value != possible_value and self.inverse_match:
                return True
        return False


class Response(object):
    """
    Corresponds to a particular response that a call might give
    """

    def __init__(self, name, response_string, content_type, status, parameters):
        """
        @param name: The name of the call
        @param response_string: The response string which should be returned
            if the response matches
        @param status: The HTTP status code which should be returned if the
            response matches
        @param parameters: List of ParameterMatch objects which must be
            matched for the response to match
        """
        self.name = name
        self.response_string = response_string
        self.content_type = content_type
        self.status = status
        self.parameters = parameters

    def generate_response(self, data_dict):
        return (self.status, self.response_string, self.content_type)

    def match(self, data_dict):
        """
        Try and match a response against POST and GET values

        @param data_dict: Dictionary taken from the POST and GET values
        @return: (HTTP status code, response string). If this response does
            not match the parameters passed in the data_dict then the status
            code will be 0
        """
        for parameter in self.parameters:
            value = data_dict.get(parameter.key)
            if type(value) == types.ListType:
                value = value[0]
            if not parameter.match(parameter.key, value):
                print '%s does not match %s' % (parameter.key, value)
                return (0, '', 'text/plain')
        # TODO: Substitute data_dict into response_string
        return self.generate_response(data_dict)


class ResponseCommand(Response):

    def __init__(self, name, response_command, content_type, status, parameters, working_dir):
        """
        @param name: The name of the call
        @param response_command: A shell command which will be run the stdout
            from which will be returned. The data_dict will be used to substitute
            into the command
        @param content_type: e.g. text/json
        @param status: The HTTP status code which should be returned if the
            response matches
        @param parameters: List of ParameterMatch objects which must be
            matched for the response to match
        """
        self.name = name
        self.response_command = string.Template(response_command)
        self.content_type = content_type
        self.status = status
        self.parameters = parameters
        self.working_dir = working_dir

    def generate_response(self, data_dict):
        command = self.response_command.safe_substitute(data_dict)
        p = subprocess.Popen(command,
                             cwd=self.working_dir,
                             shell=True,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        stdout, stderr = p.communicate()
        if p.returncode == 0:
            return (self.status, stdout, self.content_type)
        else:
            print 'Command "%s" returned non-zero exit status: %d' % (command, p.returncode)
            print stdout
            print stderr
            return (0, '', 'text/plain')


class PythonResult(object):

    def __init__(self):
        self.status = 200
        self.content_type = None


class ResponsePython(Response):

    def __init__(self, name, response_python, content_type, status, parameters, working_dir):
        """
        @param name: The name of the call
        @param response_python: The Python code to execute. The data_dict will be available
            in the global variable data
        @param content_type: e.g. text/json
        @param status: The HTTP status code which should be returned if the
            response matches
        @param parameters: List of ParameterMatch objects which must be
            matched for the response to match
        """
        self.name = name
        self.response_python = self.process_python(response_python)
        self.content_type = content_type
        self.status = status
        self.parameters = parameters
        self.working_dir = working_dir

    def process_python(self, python):
        # Since config files are trimmed of whitespace normal Python indentation
        # won't work. So instead of spaces .'s are used. If a line is prefixed
        # this .'s then these should be converted to whitespace
        return re.sub('^(\.*)', lambda match: ' ' * len(match.group(0)), python, flags=re.MULTILINE)

    def generate_response(self, data_dict):
        data = {}
        for k, v in data_dict.items():
            if type(v) == types.ListType:
                data[k] = v[0]
            else:
                data[k] = v
        # If the Python code wants to return anything then it will need to write
        # to the stream out
        out = StringIO.StringIO()
        result = PythonResult()
        # The Python code can modify the status code and content type by modifying result
        result.content_type = self.content_type
        result.status = self.status
        interpreter = code.InteractiveConsole({'data': data, 'out': out, 'result': result})
        for line in self.response_python.split('\n'):
            interpreter.push(line)
        interpreter.push('\n')
        return (result.status, out.getvalue(), result.content_type)


class Call(object):
    """
    A call represents a web service API call and is defined in a call file
    """

    def __init__(self, call_definition_file):
        """
        @param call_definition_file: The file defining the call
        """
        self.path = None
        self.path_placeholders = []
        self.method = None
        self.timeout = 0
        self.timeout_perc = 0
        self.responses = []
        self.read_definition_file(call_definition_file)

    def match(self, path, method):
        """
        @return: True if this call matches a particular path and HTTP method
        """
        return self.path.match(path) and method == self.method

    def handle(self, path, body, data_dict):
        """
        Handle the call given the HTTP body and parameters (data_dict)

        @param body: HTTP body
        @param data_dict: GET, POST and path values

        @return: (HTTP status code, response string). If no responses match
            given the data_dict then a HTTP status 500 internal server error
            will be returned
        """
        # Add any placeholders as parameters to data_dict
        placeholder_values = self.path.match(path).groups()
        for i, placeholder_value in enumerate(placeholder_values):
            data_dict[self.path_placeholders[i]] = placeholder_value

        for response in self.responses:
            status, response_string, content_type = response.match(data_dict)
            if status > 0:
                # See if a timeout should be simulated
                if random.random() < self.timeout_perc:
                    print 'Simulating timeout'
                    time.sleep(self.timeout)
                return (status, response_string, content_type)
        print '=' * 80
        print 'Error returning 500'
        print data_dict
        return (500, 'Internal server error', 'text/plain')

    def read_definition_file(self, call_definition_file):
        """
        Read the call definition file and use it to initialise this object

        @param call_definition_file: Path to the file defining the call
        """
        config = ConfigParser.RawConfigParser(
            {'timeout': 0,
             'content_type': 'text/plain',
            }
        )
        config.read(call_definition_file)

        # Read call section
        self.path = config.get('call', 'path')
        self.method = config.get('call', 'method')
        self.timeout = config.getfloat('call', 'timeout')
        if self.timeout:
            self.timeout_perc = config.getfloat('call', 'timeout_perc')
        else:
            self.timeout = 0
        response_names = [response.strip() for response in config.get('call', 'responses').split(',')]

        working_dir = os.path.dirname(call_definition_file)

        # Read the individual response sections
        for response_name in response_names:
            response = None
            response_string = None
            response_command = None
            response_python = None

            content_type = config.get(response_name, 'content_type')

            if config.has_option(response_name, 'response'):
                response_string = config.get(response_name, 'response')
            elif config.has_option(response_name, 'response_command'):
                response_command = config.get(response_name, 'response_command')
            elif config.has_option(response_name, 'response_file'):
                response_file = config.get(response_name, 'response_file')
                if response_file is not None:
                    path = os.path.join(working_dir, response_file)
                    response_string = open(path, 'rb').read()
                else:
                    print 'Response file does not exist!'
                    sys.exit(1)
            elif config.has_option(response_name, 'response_python'):
                response_python = config.get(response_name, 'response_python')

            status = config.getint(response_name, 'status')

            parameters = []
            for name, value in config.items(response_name):
                if not name.startswith('v_'):
                    continue
                key = name[2:]
                inverse_match = False
                optional = False
                if value.startswith('!'):
                    inverse_match = True
                    value = value[1:]
                elif value.startswith('~'):
                    optional = True
                    value = value[1:]
                parameter = ParameterMatch(key, value, inverse_match, optional)
                parameters.append(parameter)

            if response_string is not None:
                response = Response(response_name, response_string, content_type, status, parameters)
            elif response_command is not None:
                response = ResponseCommand(response_name, response_command, content_type, status, parameters, working_dir)
            elif response_python is not None:
                response = ResponsePython(response_name, response_python, content_type, status, parameters, working_dir)
            self.responses.append(response)

        # Convert the path into a regular expression
        self.path_placeholders = []
        for placeholder in re.findall('\$([A-Za-z_][A-Za-z0-9_\.]*)', self.path):
            self.path = self.path.replace('$' + placeholder, '([A-Za-z0-9@_\.]+)')
            self.path_placeholders.append(placeholder)
        self.path = re.compile(self.path)


class CallHandler(object):
    """
    Passes the HTTP request to the appropriate Call to be handled. If no
    appropriate Call can be found then a 500 internal server error is returned
    """

    def __init__(self, call_definition_dir):
        """
        @param call_definition_dir: The set of calls which the server will
            handle
        """
        self.call_definition_dir = call_definition_dir
        self.calls = []
        self.read_call_definitions()

    def read_call_definitions(self):
        """
        Read in all the call definitions
        """
        for definition_file in os.listdir(self.call_definition_dir):
            if definition_file.startswith('.'):
                continue
            definition_path = os.path.join(self.call_definition_dir, definition_file)
            try:
                self.calls.append(Call(definition_path))
            except:
                print 'Error reading call definition %s' % (definition_path, )
                traceback.print_exc()

    def handle_call(self, command, path, body, data_dict):
        """
        Handle a web service API call

        @param command: HTTP command - e.g. GET
        @param path: path, e.g. /login
        @param body: the request body
        @param data_dict: A dictionary of GET and POST values
        """
        for call in self.calls:
            if call.match(path, command):
                return call.handle(path, body, data_dict)
                break
        print '=' * 80
        print 'Error returning 500'
        print data_dict
        return (500, 'Internal server error', 'text/plain')


class HttpRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    """
    Handles the HTTP requests and passes them off to the call handler
    """

    def handle_request(self):
        command = self.command
        path = self.path
        content_length = self.headers.getheader('content-length')
        # For writing body to a temporary file
        tmp = tempfile.NamedTemporaryFile(delete=False)
        if content_length:
            body = self.rfile.read(int(content_length))
            data_dict = parse_qs(body)
        else:
            data_dict = {}
            body = ''
        # Write body to a temporary file
        data_dict['_body_file'] = tmp.name
        tmp.write(body)
        tmp.close()
        # Update the data dictionary with the query parameters
        data_dict.update(parse_qs(urlparse(path).query))
        path_minus_params = urlparse(path).path.strip('/')

        status, response_string, content_type = call_handler.handle_call(command, path_minus_params, body, data_dict)

        self.send_response(status)
        self.send_header('Content-type', content_type)
        self.end_headers()
        self.wfile.write(response_string)
        self.wfile.write('\n')
        self.wfile.close()

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()


def definition_change(subpath, mask):
    """
    If it is detected that one of the call definition files has changed,
    one has been deleted or one has been added then this function will
    restart the server

    @param subpath: The path where the change was detected
    @param mask
    """
    # Ignore files which end in .db
    if subpath.endswith('.db'):
        return
    print 'Definition file changed in %s' % (subpath, )
    print 'Restarting server...'
    httpd.shutdown()
    os.execv(sys.executable, [sys.executable] + sys.argv)


def main():
    usage = 'usage: %prog [options] CALL_DIRECTORY'
    parser = OptionParser(usage=usage)
    parser.add_option('-p', '--port', dest='port',
                      help='port to listen on')
    parser.add_option('-a', '--address', dest='address',
                      help='address to listen on')
    (options, args) = parser.parse_args()

    if len(args) != 1:
        parser.print_help()
        sys.exit(1)

    call_definition_path = args[0]

    if options.port:
        port = int(options.port)
    else:
        port = PORT
    if options.address:
        ip_address = options.address
    else:
        ip_address = IP_ADDRESS

    # Monitor the call definition path to restart the
    # server if any of the files change, or new ones
    # are added
    observer = Observer()
    observer.start()
    stream = Stream(definition_change, call_definition_path)
    observer.schedule(stream)

    global call_handler
    call_handler = CallHandler(call_definition_path)

    server_class = BaseHTTPServer.HTTPServer

    global httpd
    httpd = server_class((ip_address, port), HttpRequestHandler)

    print 'WebServiceSimulator started'

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print 'Shutting down web service simulator'
        httpd.server_close()
        sys.exit(0)


if __name__ == '__main__':
    main()
