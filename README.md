# Web Service Simulator

The Web Service Simulator provides a tool for producing a mock web service. It can be used whilst an app is in development without waiting for the web service to be finished.

The information in this file is a little out of date - there are currently a few undocumented features.

## Getting Started

Before using the Web Service Simulator make sure that MacFSEvents has been installed (easy_install MacFSEvents).

There is currently no easy_install or other package - for the time being copy wss.py to wherever you want to keep it, ideally somewhere in your PATH.

## Running

To run type wss.py and provide the name of a folder containing the web service calls to simulate.

By default it listens on localhost (127.0.0.1) and port 8000. The -a and -i options can be used to change the listen on address and port.

## Creating a Mock Web Service

### Overview

A Web Service Simulation is a set of files where each file represents a URL path and method. The file defines what responses the web service will return based on parameters provided via query parameters, POST data or from the URL itself.

### File Format

The file format is based on the ini file format. The only mandatory section is the [call] section. This requires the following to be defined:

 * path - this can include parameters which are prefixed with a $
 * method - e.g. GET, POST, ...
 * responses - a list of response names. Each response will have a separate section in the ini file

Optionally you can define:

 * timeout - in seconds, what represents a timeout
 * timeout_perc - as a decimal what percentage of calls will result in a timeout

Each response section defines under what conditions the response will be given and what the response is. The following must be defined:

 * the response - this must be one of:
   * response - the response string that will be provided to the caller. This could be JSON, XML, text, ...
   * response_file - a path relative to the call directory, the contents of which will be returned to the caller
   * response_command - a shell command which will be executed. Any variables will be substituted. For example ‘cat $filename’
 * status - the HTTP status code, e.g. 200

A response can also specify values which must be matched for the response to be given. Each parameter specified must be prefixed with v_. A negative match can be specified by prefixed the value with !. For example:

    v_email = mk_regp@mail.com
    v_password = !secret

will say that the response will be given in the parameter email is mk_regp@mail.com and the parameter password is anything other than secret.

The responses are matched in the order in which they appear in the file. The least specific response, e.g. one which matches no specific parameters, should be at the end of the file.

### An Example

This example defines a web service which to which two calls can be made. One to log in a user and the other to check for the availability of a screen name and email address for signing up.

The directory structure looks like:

    example_web_service/
        login
        user_availability

#### login

This call expects a username, password and secret key to be supplied. If all three are correct then a response is provided to show that the user has been logged in, otherwise a series of errors are given particular to what is incorrect - the first being the incorrect secret.

<pre>
[call]
path = session
method = POST
responses = success,incorrect_secret,invalid_username,invalid_password
timeout = 15
timeout_perc = 0.05

[success]
response = {"rsp":"Logged In","user_id":362377}
v_key = secretkey
v_username = mk_regp
v_password = password
status = 200

[incorrect_secret]
response = {"rsp":"fail","err":{"code":100,"msg":"Invalid API Key (Key not found)"}}
v_key = !ch17dc4r3
status = 404

[invalid_username]
response = {"err":"Invalid authentication credentials"}
v_username = !mk_regp
status = 404

[invalid_password]
response = {"err":"Invalid authentication credentials"}
v_username = !password
status = 404
</pre>

### user_availability

This call uses parameters supplied in the URL path to determine whether a particular screen name and email are available for user registration. The parameters are specified in the path as $screen_name and $email.

<pre>
[call]
path = user-availability/check_user_availability/$screen_name/$email
method = GET
responses = username_taken,email_taken,success
timeout = 15
timeout_perc = 0.05

[username_taken]
response = {"rsp":"The chosen username has already been taken. Please choose another."}
v_screen_name = mk_regp
status = 401

[email_taken]
response = {"rsp":"The chosen email has already been taken. Please choose another."}
v_email = mk_regp@mail.com
status = 401

[success]
response = {"rsp":"OK"}
status = 200
</pre>