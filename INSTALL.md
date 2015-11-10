Installing the web control widget
=================================

The web control widget is a Node.js server.  It can be run on any
machine which can connect to the ZeroMQ sockets of `codplayerd`.

When installed, open `http://localhost:8304/` if running locally
(otherwise substitute the hostname and possibly port number with the
correct location).


Dependencies
------------

The control widget has been tested with Node.js 0.10.x.


Install released package
------------------------

The latest release of the control widget can be installed with npm.
It is recommended to install it in a dedicated directory, e.g.:

    mkdir ~/cod
    cd ~/cod
    npm install codplayer-control


Configuration
-------------

The default settings in `config.json` match the default ZeroMQ
settings in `codplayer.conf`, but might have to be changed if you are
running on different machines or have changed the ports.  It's best to
do this by copying the file somewhere else and change the settings,
then give the filename to `codplayer-control` on the command line.


Running package installation
----------------------------

Assuming the control widget was installed in `~/cod` and using the
default configuration, it can be run like this:

    nohup ~/cod/node_modules/.bin/codplayer-control >> ~/cod/codctlwidget.log 2>&1 &

The server does not fork (right now), so that's why it is run with
nohup and in the background

If you have changed the configuration, the path to the new config can
be proved on the command line:

    ~/cod/node_modules/.bin/codplayer-control /path/to/config.json


Running from source dir
-----------------------

To just run the server from the source directory, the dependencies
must first be installed:

    npm install

Then run the server with either

    ./server.js [/path/to/config.json]

or
    /path/to/node server.js [/path/to/config.json]
