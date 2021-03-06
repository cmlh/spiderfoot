#-----------------------------------------------------------------
# Name:         sfscan
# Purpose:      Scanning control functionality
#
# Author:       Steve Micallef <steve@binarypool.com>
#
# Created:      11/03/2013
# Copyright:    (c) Steve Micallef 2013
# License:      GPL
#-----------------------------------------------------------------
import json
import traceback
import os
import time
import sys
import socket
import socks
import dns.resolver
from copy import deepcopy
from sfdb import SpiderFootDb
from sflib import SpiderFoot, SpiderFootEvent

# Controls all scanning activity
# Eventually change this to be able to control multiple scan instances
class SpiderFootScanner:
    moduleInstances = None
    status = "UNKNOWN"
    myId = None

    def __init__(self, name, target, moduleList, globalOpts, moduleOpts):
        self.config = deepcopy(globalOpts)
        self.sf = SpiderFoot(self.config)
        self.target = target
        self.moduleList = moduleList
        self.name = name

        return

    # Status of the currently running scan (if any)
    def scanStatus(self, id):
        if id != self.myId:
            return "UNKNOWN"
        return self.status  

    # Stop a scan (id variable is unnecessary for now given that only one simultaneous
    # scan is permitted.)
    def stopScan(self, id):
        if id != self.myId:
            return None

        if self.moduleInstances == None:
            return None

        for modName in self.moduleInstances.keys():
            self.moduleInstances[modName].stopScanning()

    # Start running a scan
    def startScan(self):
        self.moduleInstances = dict()
        dbh = SpiderFootDb(self.config)
        self.sf.setDbh(dbh)
        aborted = False

        # Create a unique ID for this scan and create it in the back-end DB.
        self.config['__guid__'] = dbh.scanInstanceGenGUID(self.target)
        self.sf.setScanId(self.config['__guid__'])
        self.myId = self.config['__guid__']
        dbh.scanInstanceCreate(self.config['__guid__'], self.name, self.target)
        dbh.scanInstanceSet(self.config['__guid__'], time.time() * 1000, None, 'STARTING')
        self.status = "STARTING"
        
        # Save the config current set for this scan
        self.config['_modulesenabled'] = self.moduleList
        dbh.scanConfigSet(self.config['__guid__'], self.sf.configSerialize(self.config))

        self.sf.status("Scan [" + self.config['__guid__'] + "] initiated.")
        # moduleList = list of modules the user wants to run
        try:
            # Process global options that point to other places for data

            # If a SOCKS server was specified, set it up
            if self.config['_socks1type'] != '':
                socksType = socks.PROXY_TYPE_SOCKS4
                socksDns = self.config['_socks6dns']
                socksAddr = self.config['_socks2addr']
                socksPort = int(self.config['_socks3port'])
                socksUsername = ''
                socksPassword = ''

                if self.config['_socks1type'] == '4':
                    socksType = socks.PROXY_TYPE_SOCKS4
                if self.config['_socks1type'] == '5':
                    socksType = socks.PROXY_TYPE_SOCKS5
                    socksUsername = self.config['_socks4user']
                    socksPassword = self.config['_socks5pwd']
                    
                if self.config['_socks1type'] == 'HTTP':
                    socksType = socks.PROXY_TYPE_HTTP
                   
                self.sf.debug("SOCKS: " + socksAddr + ":" + str(socksPort) + \
                    "(" + socksUsername + ":" + socksPassword + ")")
                socks.setdefaultproxy(socksType, socksAddr, socksPort, 
                    socksDns, socksUsername, socksPassword)

                # Override the default socket and getaddrinfo calls with the 
                # SOCKS ones
                socket.socket = socks.socksocket
                socket.create_connection = socks.create_connection
                socket.getaddrinfo = socks.getaddrinfo

                self.sf.updateSocket(socket)
            
            # Override the default DNS server
            if self.config['_dnsserver'] != "":
                res = dns.resolver.Resolver()
                res.nameservers = [ self.config['_dnsserver'] ]
                dns.resolver.override_system_resolver(res)
            else:
                dns.resolver.restore_system_resolver()

            # Set the user agent
            self.config['_useragent'] = self.sf.optValueToData(self.config['_useragent'])

            # Get internet TLDs
            tlddata = self.sf.cacheGet("internet_tlds", self.config['_internettlds_cache'])
            # If it wasn't loadable from cache, load it from scratch
            if tlddata == None:
                self.config['_internettlds'] = self.sf.optValueToData(self.config['_internettlds'])
                self.sf.cachePut("internet_tlds", self.config['_internettlds'])
            else:
                self.config["_internettlds"] = tlddata.splitlines()

            for modName in self.moduleList:
                if modName == '':
                    continue

                module = __import__('modules.' + modName, globals(), locals(), [modName])
                mod = getattr(module, modName)()
                mod.__name__ = modName

                # A bit hacky: we pass the database object as part of the config. This
                # object should only be used by the internal SpiderFoot modules writing
                # to the database, which at present is only sfp__stor_db.
                # Individual modules cannot create their own SpiderFootDb instance or
                # we'll get database locking issues, so it all goes through this.
                self.config['__sfdb__'] = dbh

                # Set up the module
                # Configuration is a combined global config with module-specific options
                #modConfig = deepcopy(self.config)
                modConfig = self.config['__modules__'][modName]['opts']
                for opt in self.config.keys():
                    modConfig[opt] = self.config[opt]

                mod.clearListeners() # clear any listener relationships from the past
                mod.setup(self.sf, self.target, modConfig)
                self.moduleInstances[modName] = mod

                # Override the module's local socket module
                # to be the SOCKS one.
                if self.config['_socks1type'] != '':
                    mod._updateSocket(socket)

                self.sf.status(modName + " module loaded.")

            # Register listener modules and then start all modules sequentially
            for module in self.moduleInstances.values():
                for listenerModule in self.moduleInstances.values():
                    # Careful not to register twice or you will get duplicate events
                    if listenerModule in module._listenerModules:
                        continue
                    # Note the absence of a check for whether a module can register
                    # to itself. That is intentional because some modules will
                    # act on their own notifications (e.g. sfp_dns)!
                    if listenerModule.watchedEvents() != None:
                        module.registerListener(listenerModule)

            dbh.scanInstanceSet(self.config['__guid__'], status='RUNNING')
            self.status = "RUNNING"

            # Create the "ROOT" event which un-triggered modules will link events to
            rootEvent = SpiderFootEvent("INITIAL_TARGET", self.target, "SpiderFoot UI")
            dbh.scanEventStore(self.config['__guid__'], rootEvent)

            # Start the modules sequentially.
            for module in self.moduleInstances.values():
                # Check in case the user requested to stop the scan between modules initializing
                if module.checkForStop():
                    dbh.scanInstanceSet(self.config['__guid__'], status='ABORTING')
                    self.status = "ABORTING"
                    aborted = True
                    break
                # Many modules' start() method will return None, as most will rely on 
                # notifications during the scan from other modules.
                module.start()

            # Check if any of the modules ended due to being stopped
            for module in self.moduleInstances.values():
                if module.checkForStop():
                    aborted = True

            if aborted:
                self.sf.status("Scan [" + self.config['__guid__'] + "] aborted.")
                dbh.scanInstanceSet(self.config['__guid__'], None, time.time() * 1000, 'ABORTED')
                self.status = "ABORTED"
            else:
                self.sf.status("Scan [" + self.config['__guid__'] + "] completed.")
                dbh.scanInstanceSet(self.config['__guid__'], None, time.time() * 1000, 'FINISHED')
                self.status = "FINISHED"
        except BaseException as e:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            self.sf.error("Unhandled exception (" + e.__class__.__name__ + ") " + \
                "encountered during scan. Please report this as a bug: " + \
                repr(traceback.format_exception(exc_type, exc_value, exc_traceback)), False)
            self.sf.status("Scan [" + self.config['__guid__'] + "] failed: " + str(e))
            dbh.scanInstanceSet(self.config['__guid__'], None, time.time() * 1000, 'ERROR-FAILED')
            self.status = "ERROR-FAILED"

        self.moduleInstances = None
        dbh.close()
        self.sf.setDbh(None)
        self.sf.setScanId(None)

