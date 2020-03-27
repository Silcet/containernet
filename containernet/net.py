"""

    Mininet: A simple networking testbed for OpenFlow/SDN!

author: Bob Lantz (rlantz@cs.stanford.edu)
author: Brandon Heller (brandonh@stanford.edu)

Mininet creates scalable OpenFlow test networks by using
process-based virtualization and network namespaces.

Simulated hosts are created as processes in separate network
namespaces. This allows a complete OpenFlow network to be simulated on
top of a single Linux kernel.

Each host has:

A virtual console (pipes to a shell)
A virtual interfaces (half of a veth pair)
A parent shell (and possibly some child processes) in a namespace

Hosts have a network interface which is configured via ifconfig/ip
link/etc.

This version supports both the kernel and user space datapaths
from the OpenFlow reference implementation (openflowswitch.org)
as well as OpenVSwitch (openvswitch.org.)

In kernel datapath mode, the controller and switches are simply
processes in the root namespace.

Kernel OpenFlow datapaths are instantiated using dpctl(8), and are
attached to the one side of a veth pair; the other side resides in the
host namespace. In this mode, switch processes can simply connect to the
controller via the loopback interface.

In user datapath mode, the controller and switches can be full-service
nodes that live in their own network namespaces and have management
interfaces and IP addresses on a control network (e.g. 192.168.123.1,
currently routed although it could be bridged.)

In addition to a management interface, user mode switches also have
several switch interfaces, halves of veth pairs whose other halves
reside in the host nodes that the switches are connected to.

Consistent, straightforward naming is important in order to easily
identify hosts, switches and controllers, both from the CLI and
from program code. Interfaces are named to make it easy to identify
which interfaces belong to which node.

The basic naming scheme is as follows:

    Host nodes are named h1-hN
    Switch nodes are named s1-sN
    Controller nodes are named c0-cN
    Interfaces are named {nodename}-eth0 .. {nodename}-ethN

Note: If the network topology is created using mininet.topo, then
node numbers are unique among hosts and switches (e.g. we have
h1..hN and SN..SN+M) and also correspond to their default IP addresses
of 10.x.y.z/8 where x.y.z is the base-256 representation of N for
hN. This mapping allows easy determination of a node's IP
address from its name, e.g. h1 -> 10.0.0.1, h257 -> 10.0.1.1.

Note also that 10.0.0.1 can often be written as 10.1 for short, e.g.
"ping 10.1" is equivalent to "ping 10.0.0.1".

Currently we wrap the entire network in a 'mininet' object, which
constructs a simulated network based on a network topology created
using a topology object (e.g. LinearTopo) from mininet.topo or
mininet.topolib, and a Controller which the switches will connect
to. Several configuration options are provided for functions such as
automatically setting MAC addresses, populating the ARP table, or
even running a set of terminals to allow direct interaction with nodes.

After the network is created, it can be started using start(), and a
variety of useful tasks maybe performed, including basic connectivity
and bandwidth tests and running the mininet CLI.

Once the network is up and running, test code can easily get access
to host and switch objects which can then be used for arbitrary
experiments, typically involving running a series of commands on the
hosts.

After all desired tests or activities have been completed, the stop()
method may be called to shut down the network.

"""

import re
import select
import random
import shlex
import ipaddress
from subprocess import Popen

from time import sleep
from itertools import chain, groupby
from math import ceil
from six import string_types

from mininet.net import Mininet
from mininet.link import TCULink
from mininet.log import info, error, debug, output, warn
from mininet.node import ( Node, DefaultController, Controller, OVSBridge )
from mininet.nodelib import NAT
from mininet.util import ( quietRun, fixLimits, ensureRoot, numCores,
                           macColonHex, ipStr, ipParse, ipAdd,
                           waitListening, BaseString, netParse )
from containernet.cli import CLI
from containernet.node import Docker, OVSKernelSwitch, Host, OVSSwitch
from containernet.link import TCLink, Intf

from mn_wifi.net import Mininet_wifi
from mn_wifi.node import AP, Station, Car, OVSKernelAP
from mn_wifi.util import netParse6
from mn_wifi.wmediumdConnector import snr, interference
from mn_wifi.link import wmediumd, _4address, TCWirelessLink, ITSLink,\
    wifiDirectLink, adhoc, mesh, physicalMesh, physicalWifiDirectLink
from mn_wifi.mobility import Mobility as mob
from mn_wifi.sixLoWPAN.link import sixLoWPAN


# Mininet version: should be consistent with README and LICENSE
VERSION = "2.3.0d6"
CONTAINERNET_VERSION = "3.0"

# If an external SAP (Service Access Point) is made, it is deployed with this prefix in the name,
# so it can be removed at a later time
SAP_PREFIX = 'sap.'


class Containernet( Mininet_wifi ):
    "Network emulation with hosts spawned in network namespaces."

    def __init__( self, topo=None, switch=OVSKernelSwitch,
                  accessPoint=OVSKernelAP, host=Host, station=Station,
                  car=Car, controller=DefaultController,
                  link=TCWirelessLink, intf=Intf, build=True, xterms=False,
                  cleanup=False, ipBase='10.0.0.0/8', ip6Base='2001:0:0:0:0:0:0:0/64',
                  inNamespace=False, autoSetMacs=False, autoStaticArp=False,
                  autoPinCpus=False, listenPort=None, waitConnected=False,
                  wmediumd_mode=snr, roads=0, fading_cof=0, autoAssociation=True,
                  allAutoAssociation=True, configWiFiDirect=False,
                  config4addr=False, noise_th=-91, cca_th=-90,
                  disable_tcp_checksum=False, ifb=False,
                  bridge=False, plot=False, plot3d=False, docker=False,
                  container='mininet-wifi', ssh_user='alpha',
                  set_socket_ip=None, set_socket_port=12345 ):
        """Create Mininet object.
           topo: Topo (topology) object or None
           switch: default Switch class
           host: default Host class/constructor
           controller: default Controller class/constructor
           link: default Link class/constructor
           intf: default Intf class/constructor
           ipBase: base IP address for hosts,
           build: build now from topo?
           xterms: if build now, spawn xterms?
           cleanup: if build now, cleanup before creating?
           inNamespace: spawn switches and controller in net namespaces?
           autoSetMacs: set MAC addrs automatically like IP addresses?
           autoStaticArp: set all-pairs static MAC addrs?
           autoPinCpus: pin hosts to (real) cores (requires CPULimitedHost)?
           listenPort: base listening port to open; will be incremented for
               each additional switch in the net if inNamespace=False"""

        super(Containernet, self).__init__()
        self.topo = topo
        self.switch = switch
        self.host = host
        self.station = station
        self.accessPoint = accessPoint
        self.controller = controller
        self.link = link
        self.intf = intf
        self.ipBase = ipBase
        self.ip6Base = ip6Base
        self.ipBaseNum, self.prefixLen = netParse(self.ipBase)
        self.ip6BaseNum, self.prefixLen6 = netParse6(self.ip6Base)
        hostIP = (0xffffffff >> self.prefixLen) & self.ipBaseNum
        # Start for address allocation
        self.nextIP = hostIP if hostIP > 0 else 1
        self.nextIP6 = 1
        self.inNamespace = inNamespace
        self.xterms = xterms
        self.cleanup = cleanup
        self.autoSetMacs = autoSetMacs
        self.autoStaticArp = autoStaticArp
        self.autoPinCpus = autoPinCpus
        self.numCores = numCores()
        self.nextCore = 0  # next core for pinning hosts to CPUs
        self.listenPort = listenPort
        self.waitConn = waitConnected
        self.channel = 1
        self.mode = 'g'
        self.autoSetPositions = ''
        self.nextPos_sta = 1
        self.n_radios = 0
        self.hosts = []
        self.switches = []
        self.aps = []
        self.cars = []
        self.sixLP = []
        self.controllers = []
        self.stations = []
        self.apsensors = []
        self.sensors = []
        self.wmediumdMac = []
        self.autoAssociation = autoAssociation  # does not include mobility
        self.allAutoAssociation = allAutoAssociation  # includes mobility
        self.noise_threshold = -91
        self.cca_th = -90
        self.driver = 'nl80211'
        self.ssid = 'new-ssid'
        self.config4addr = False
        self.configWiFiDirect = False
        self.wmediumd_mode = wmediumd_mode
        self.isVanet = False
        self.bridge = False
        self.ifb = False
        self.alt_module = False
        self.ppm_is_set = False
        self.docker = False
        self.draw = False
        self.isReplaying = False
        self.wmediumd_started = False
        self.mob_check = False
        self.isVanet = False
        self.links = []
        self.mob_param = {}
        self.nameToNode = {}  # name to Node (Host/Switch) objects
        self.terms = []  # list of spawned xterm processes
        self.SAPswitches = {}
        self.set_socket_ip = set_socket_ip
        self.set_socket_port = set_socket_port
        self.docker = docker
        self.container = container
        self.ssh_user = ssh_user
        self.ifb = ifb  # Support to Intermediate Functional Block (IFB) Devices
        self.bridge = bridge
        self.init_plot = plot
        self.init_plot3d = plot3d
        self.cca_th = cca_th
        self.configWiFiDirect = configWiFiDirect
        self.config4addr = config4addr
        self.fading_cof = fading_cof
        self.noise_th = noise_th
        self.mob_param = dict()
        self.disable_tcp_checksum = disable_tcp_checksum
        self.roads = roads
        self.seed = 1
        self.n_radios = 0
        self.min_v = 1
        self.max_v = 10
        self.min_x = 0
        self.min_y = 0
        self.min_z = 0
        self.max_x = 100
        self.max_y = 100
        self.max_z = 0
        self.conn = {}
        self.wlinks = []
        # Mininet_wifi.init()  # Initialize Mininet if necessary

        if self.set_socket_ip:
            self.server()

        if self.autoSetPositions and self.link == wmediumd:
            self.wmediumd_mode = interference

        if not self.allAutoAssociation:
            self.autoAssociation = False
            mob.allAutoAssociation = False

        self.built = False
        if self.topo and self.build:
            self.build()

    def waitConnected( self, timeout=None, delay=.5 ):
        """wait for each switch to connect to a controller,
           up to 5 seconds
           timeout: time to wait, or None to wait indefinitely
           delay: seconds to sleep per iteration
           returns: True if all switches are connected"""
        info( '*** Waiting for switches to connect\n' )
        time = 0
        remaining = list( self.switches )
        while True:
            for switch in tuple( remaining ):
                if switch.connected():
                    info( '%s ' % switch )
                    remaining.remove( switch )
            if not remaining:
                info( '\n' )
                return True
            if timeout is not None and time > timeout:
                break
            sleep( delay )
            time += delay
        warn( 'Timed out after %d seconds\n' % time )
        for switch in remaining:
            if not switch.connected():
                warn( 'Warning: %s is not connected to a controller\n'
                      % switch.name )
            else:
                remaining.remove( switch )
        return not remaining

    def getNextIp( self ):
        ip = ipAdd( self.nextIP,
                    ipBaseNum=self.ipBaseNum,
                    prefixLen=self.prefixLen ) + '/%s' % self.prefixLen
        self.nextIP += 1
        return ip

    def removeHost( self, name, **params):
        """
        Remove a host from the network at runtime.
        """
        if not isinstance( name, BaseString ) and name is not None:
            name = name.name  # if we get a host object
        try:
            n = self.get(name)
        except:
            error("Host: %s not found. Cannot remove it.\n" % name)
            return False
        if n is not None:
            if n in self.hosts:
                self.hosts.remove(n)
            if n in self.stations:
                self.stations.remove(n)
            if name in self.nameToNode:
                del self.nameToNode[name]
            n.stop( deleteIntfs=True )
            debug("Removed: %s\n" % name)
            return True
        return False

    def delNode( self, node, nodes=None):
        """Delete node
           node: node to delete
           nodes: optional list to delete from (e.g. self.hosts)"""
        if nodes is None:
            nodes = ( self.hosts if node in self.hosts else
                      (self.stations if node in self.stations else
                       (self.aps if node in self.aps else
                        ( self.switches if node in self.switches else
                         ( self.controllers if node in self.controllers else
                              [] ) ) ) ) )
        node.stop( deleteIntfs=True )
        node.terminate()
        nodes.remove( node )
        del self.nameToNode[ node.name ]

    def delHost( self, host ):
        "Delete a host"
        self.delNode( host, nodes=self.hosts )

    def delSwitch( self, switch ):
        "Delete a switch"
        self.delNode( switch, nodes=self.switches )

    def addNAT( self, name='nat0', connect=True, inNamespace=False,
                **params):
        """Add a NAT to the Mininet network
           name: name of NAT node
           connect: switch to connect to | True (s1) | None
           inNamespace: create in a network namespace
           params: other NAT node params, notably:
               ip: used as default gateway address"""
        nat = self.addHost( name, cls=NAT, inNamespace=inNamespace,
                            subnet=self.ipBase, **params )
        # find first switch and create link
        if connect:
            if not isinstance( connect, Node ):
                # Use first switch if not specified
                connect = self.switches[ 0 ]
            # Connect the nat to the switch
            self.addLink( nat, connect )
            # Set the default route on hosts
            natIP = nat.params[ 'ip' ].split('/')[ 0 ]
            for host in self.hosts:
                if host.inNamespace:
                    host.setDefaultRoute( 'via %s' % natIP )
        return nat

    # BL: We now have four ways to look up nodes
    # This may (should?) be cleaned up in the future.
    def getNodeByName( self, *args ):
        "Return node(s) with given name(s)"
        if len( args ) == 1:
            return self.nameToNode[ args[ 0 ] ]
        return [ self.nameToNode[ n ] for n in args ]

    def get( self, *args ):
        "Convenience alias for getNodeByName"
        return self.getNodeByName( *args )

    # Even more convenient syntax for node lookup and iteration
    def __getitem__( self, key ):
        "net[ name ] operator: Return node with given name"
        return self.nameToNode[ key ]

    def __delitem__( self, key ):
        "del net[ name ] operator - delete node with given name"
        self.delNode( self.nameToNode[ key ] )

    def __iter__( self ):
        "return iterator over node names"
        for node in chain( self.hosts, self.stations, self.aps, self.switches, self.controllers ):
            yield node.name

    def __len__( self ):
        "returns number of nodes in net"
        return ( len( self.hosts ) + len( self.stations ) + len( self.switches ) +
                 len( self.aps ) + len( self.controllers ) )

    def __contains__( self, item ):
        "returns True if net contains named node"
        return item in self.nameToNode

    def keys( self ):
        "return a list of all node names or net's keys"
        return list( self )

    def values( self ):
        "return a list of all nodes or net's values"
        return [ self[name] for name in self ]

    def items( self ):
        "return (key,value) tuple list for every node in net"
        return zip( self.keys(), self.values() )

    @staticmethod
    def randMac():
        "Return a random, non-multicast MAC address"
        return macColonHex( random.randint(1, 2**48 - 1) & 0xfeffffffffff |
                            0x020000000000 )

    def addLink(self, node1, node2=None, port1=None, port2=None,
                cls=None, **params):
        """"Add a link from node1 to node2
            node1: source node (or name)
            node2: dest node (or name)
            port1: source port (optional)
            port2: dest port (optional)
            cls: link class (optional)
            params: additional link params (optional)
            returns: link object"""

        # Accept node objects or names
        node1 = node1 if not isinstance(node1, string_types) else self[node1]
        node2 = node2 if not isinstance(node2, string_types) else self[node2]
        options = dict(params)

        self.conn.setdefault('src', [])
        self.conn.setdefault('dst', [])
        self.conn.setdefault('ls', [])

        cls = self.link if cls is None else cls

        modes = [mesh, physicalMesh, adhoc, ITSLink,
                 wifiDirectLink, physicalWifiDirectLink]
        if cls in modes:
            cls(node=node1, **params)
        elif cls == sixLoWPAN:
            link = cls(node=node1, port=port1, **params)
            self.links.append(link)
            return link
        elif cls == _4address:
            if 'position' in node1.params and 'position' in node2.params:
                self.conn['src'].append(node1)
                self.conn['dst'].append(node2)
                self.conn['ls'].append('--')

            if node1 not in self.aps:
                self.aps.append(node1)
            elif node2 not in self.aps:
                self.aps.append(node2)

            if self.wmediumd_mode == interference:
                link = cls(node1, node2, port1, port2)
                self.links.append(link)
                return link
            else:
                dist = node1.get_distance_to(node2)
                if dist <= node1.params['range'][0]:
                    link = cls(node1, node2)
                    self.links.append(link)
                    return link
        elif ((node1 in self.stations and node2 in self.aps)
              or (node2 in self.stations and node1 in self.aps)) and cls != TCLink:
            if cls == wmediumd:
                self.infra_wmediumd_link(node1, node2, **params)
            else:
                self.infra_tc(node1, node2, port1, port2, cls, **params)
        else:
            if 'link' in options:
                options.pop('link', None)

            if 'position' in node1.params and 'position' in node2.params:
                self.conn['src'].append(node1)
                self.conn['dst'].append(node2)
                self.conn['ls'].append('-')
            # Port is optional
            if port1 is not None:
                options.setdefault('port1', port1)
            if port2 is not None:
                options.setdefault('port2', port2)

            # Set default MAC - this should probably be in Link
            options.setdefault('addr1', self.randMac())
            options.setdefault('addr2', self.randMac())

            if not cls or cls == wmediumd or cls == TCWirelessLink:
                cls = TCLink
            if self.disable_tcp_checksum:
                cls = TCULink

            cls = self.link if cls is None else cls
            link = cls(node1, node2, **options)

            # Allow to add links at runtime
            # (needs attach method provided by OVSSwitch)
            if isinstance(node1, OVSSwitch) or isinstance(node1, AP):
                node1.attach(link.intf1)
            if isinstance(node2, OVSSwitch) or isinstance(node2, AP):
                node2.attach(link.intf2)

            self.links.append(link)
            return link

    def removeLink(self, link=None, node1=None, node2=None):
        """
        Removes a link. Can either be specified by link object,
        or the nodes the link connects.
        """
        if link is None:
            if (isinstance( node1, BaseString )
                    and isinstance( node2, BaseString )):
                try:
                    node1 = self.get(node1)
                except:
                    error("Host: %s not found.\n" % node1)
                try:
                    node2 = self.get(node2)
                except:
                    error("Host: %s not found.\n" % node2)
            # try to find link by nodes
            for l in self.links:
                if l.intf1.node == node1 and l.intf2.node == node2:
                    link = l
                    break
                if l.intf1.node == node2 and l.intf2.node == node1:
                    link = l
                    break
        if link is None:
            error("Couldn't find link to be removed.\n")
            return
        # tear down the link
        link.delete()
        self.links.remove(link)

    def delLink( self, link ):
        "Remove a link from this network"
        link.delete()
        self.links.remove( link )

    def linksBetween( self, node1, node2 ):
        "Return Links between node1 and node2"
        return [ link for link in self.links
                 if ( node1, node2 ) in (
                    ( link.intf1.node, link.intf2.node ),
                    ( link.intf2.node, link.intf1.node ) ) ]

    def delLinkBetween( self, node1, node2, index=0, allLinks=False ):
        """Delete link(s) between node1 and node2
           index: index of link to delete if multiple links (0)
           allLinks: ignore index and delete all such links (False)
           returns: deleted link(s)"""
        links = self.linksBetween( node1, node2 )
        if not allLinks:
            links = [ links[ index ] ]
        for link in links:
            self.delLink( link )
        return links

    def buildFromTopo( self, topo=None ):
        """Build mininet from a topology object
           At the end of this function, everything should be connected
           and up."""

        # Possibly we should clean up here and/or validate
        # the topo
        if self.cleanup:
            pass

        info( '*** Creating network\n' )

        if not self.controllers and self.controller:
            # Add a default controller
            info( '*** Adding controller\n' )
            classes = self.controller
            if not isinstance( classes, list ):
                classes = [ classes ]
            for i, cls in enumerate( classes ):
                # Allow Controller objects because nobody understands partial()
                if isinstance( cls, Controller ):
                    self.addController( cls )
                else:
                    self.addController( 'c%d' % i, cls )

        info( '*** Adding hosts:\n' )
        for hostName in topo.hosts():
            self.addHost( hostName, **topo.nodeInfo( hostName ) )
            info( hostName + ' ' )

        info( '\n*** Adding switches:\n' )
        for switchName in topo.switches():
            # A bit ugly: add batch parameter if appropriate
            params = topo.nodeInfo( switchName)
            cls = params.get( 'cls', self.switch )
            #if hasattr( cls, 'batchStartup' ):
            #    params.setdefault( 'batch', True )
            self.addSwitch( switchName, **params )
            info( switchName + ' ' )

        info( '\n*** Adding links:\n' )
        for srcName, dstName, params in topo.links(
                sort=True, withInfo=True ):
            self.addLink( **params )
            info( '(%s, %s) ' % ( srcName, dstName ) )

        info( '\n' )

    def configureControlNetwork( self ):
        "Control net config hook: override in subclass"
        raise Exception( 'configureControlNetwork: '
                         'should be overriden in subclass', self )

    def stop( self ):
        self.stop_graph_params()
        info('*** Removing NAT rules of %i SAPs\n' % len(self.SAPswitches))
        for SAPswitch in self.SAPswitches:
            self.removeSAPNAT(self.SAPswitches[SAPswitch])
        info("\n")
        "Stop the controller(s), switches and hosts"
        info( '*** Stopping %i controllers\n' % len( self.controllers ) )
        for controller in self.controllers:
            info( controller.name + ' ' )
            controller.stop()
        info( '\n' )
        if self.terms:
            info( '*** Stopping %i terms\n' % len( self.terms ) )
            self.stopXterms()
        info( '*** Stopping %i links\n' % len( self.links ) )
        for link in self.links:
            info( '.' )
            link.stop()
        info( '\n' )
        nodesL2 = self.switches + self.aps
        info( '*** Stopping %i switches\n' % len( nodesL2 ) )
        stopped = {}
        for swclass, switches in groupby(
                sorted(nodesL2, key=type), type):
            switches = tuple(switches)
            if hasattr(swclass, 'batchShutdown'):
                success = swclass.batchShutdown(switches)
                stopped.update({s: s for s in success})
        for switch in nodesL2:
            info(switch.name + ' ')
            if switch not in stopped:
                switch.stop()
            switch.terminate()
        info( '\n' )
        nodes = self.hosts + self.stations
        info( '*** Stopping %i hosts/stations\n' % len( nodes ) )
        for node in nodes:
            info( node.name + ' ' )
            node.terminate()
        self.closeMininetWiFi()
        info( '\n*** Done\n' )

    def run( self, test, *args, **kwargs ):
        "Perform a complete start/test/stop cycle."
        self.start()
        info( '*** Running test\n' )
        result = test( *args, **kwargs )
        self.stop()
        return result

    def monitor( self, hosts=None, timeoutms=-1 ):
        """Monitor a set of hosts (or all hosts by default),
           and return their output, a line at a time.
           hosts: (optional) set of hosts to monitor
           timeoutms: (optional) timeout value in ms
           returns: iterator which returns host, line"""
        if hosts is None:
            hosts = self.hosts
        poller = select.poll()
        h1 = hosts[ 0 ]  # so we can call class method fdToNode
        for host in hosts:
            poller.register( host.stdout )
        while True:
            ready = poller.poll( timeoutms )
            for fd, event in ready:
                host = h1.fdToNode( fd )
                if event & select.POLLIN:
                    line = host.readline()
                    if line is not None:
                        yield host, line
            # Return if non-blocking
            if not ready and timeoutms >= 0:
                yield None, None

    # XXX These test methods should be moved out of this class.
    # Probably we should create a tests.py for them

    @staticmethod
    def _parsePing( pingOutput ):
        "Parse ping output and return packets sent, received."
        # Check for downed link
        if 'connect: Network is unreachable' in pingOutput:
            return 1, 0
        r = r'(\d+) packets transmitted, (\d+)( packets)? received'
        m = re.search( r, pingOutput )
        if m is None:
            error( '*** Error: could not parse ping output: %s\n' %
                   pingOutput )
            return 1, 0
        sent, received = int( m.group( 1 ) ), int( m.group( 2 ) )
        return sent, received

    def ping( self, hosts=None, timeout=None, manualdestip=None ):
        """Ping between all specified hosts.
           hosts: list of hosts
           timeout: time to wait for a response, as string
           manualdestip: sends pings from each h in hosts to manualdestip
           returns: ploss packet loss percentage"""
        # should we check if running?
        packets = 0
        lost = 0
        ploss = None
        if not hosts:
            hosts = self.hosts
            output( '*** Ping: testing ping reachability\n' )
        for node in hosts:
            output( '%s -> ' % node.name )
            if manualdestip is not None:
                opts = ''
                if timeout:
                    opts = '-W %s' % timeout
                result = node.cmd( 'ping -c1 %s %s' %
                                   (opts, manualdestip) )
                sent, received = self._parsePing( result )
                packets += sent
                if received > sent:
                    error( '*** Error: received too many packets' )
                    error( '%s' % result )
                    node.cmdPrint( 'route' )
                    exit( 1 )
                lost += sent - received
                output( ( '%s ' % manualdestip ) if received else 'X ' )
            else:
                for dest in hosts:
                    if node != dest:
                        opts = ''
                        if timeout:
                            opts = '-W %s' % timeout
                        if dest.intfs:
                            result = node.cmd( 'ping -c1 %s %s' %
                                               (opts, dest.IP()) )
                            sent, received = self._parsePing( result )
                        else:
                            sent, received = 0, 0
                        packets += sent
                        if received > sent:
                            error( '*** Error: received too many packets' )
                            error( '%s' % result )
                            node.cmdPrint( 'route' )
                            exit( 1 )
                        lost += sent - received
                        output( ( '%s ' % dest.name ) if received else 'X ' )
            output( '\n' )
        if packets > 0:
            ploss = 100.0 * lost / packets
            received = packets - lost
            output( "*** Results: %i%% dropped (%d/%d received)\n" %
                    ( ploss, received, packets ) )
        else:
            ploss = 0
            output( "*** Warning: No packets sent\n" )
        return ploss

    @staticmethod
    def _parsePingFull( pingOutput ):
        "Parse ping output and return all data."
        errorTuple = (1, 0, 0, 0, 0, 0)
        # Check for downed link
        r = r'[uU]nreachable'
        m = re.search( r, pingOutput )
        if m is not None:
            return errorTuple
        r = r'(\d+) packets transmitted, (\d+)( packets)? received'
        m = re.search( r, pingOutput )
        if m is None:
            error( '*** Error: could not parse ping output: %s\n' %
                   pingOutput )
            return errorTuple
        sent, received = int( m.group( 1 ) ), int( m.group( 2 ) )
        r = r'rtt min/avg/max/mdev = '
        r += r'(\d+\.\d+)/(\d+\.\d+)/(\d+\.\d+)/(\d+\.\d+) ms'
        m = re.search( r, pingOutput )
        if m is None:
            if received == 0:
                return errorTuple
            error( '*** Error: could not parse ping output: %s\n' %
                   pingOutput )
            return errorTuple
        rttmin = float( m.group( 1 ) )
        rttavg = float( m.group( 2 ) )
        rttmax = float( m.group( 3 ) )
        rttdev = float( m.group( 4 ) )
        return sent, received, rttmin, rttavg, rttmax, rttdev

    def pingFull( self, hosts=None, timeout=None, manualdestip=None ):
        """Ping between all specified hosts and return all data.
           hosts: list of hosts
           timeout: time to wait for a response, as string
           returns: all ping data; see function body."""
        # should we check if running?
        # Each value is a tuple: (src, dsd, [all ping outputs])
        all_outputs = []
        if not hosts:
            hosts = self.hosts
            output( '*** Ping: testing ping reachability\n' )
        for node in hosts:
            output( '%s -> ' % node.name )
            if manualdestip is not None:
                opts = ''
                if timeout:
                    opts = '-W %s' % timeout
                result = node.cmd( 'ping -c1 %s %s' % (opts, manualdestip) )
                outputs = self._parsePingFull( result )
                sent, received, rttmin, rttavg, rttmax, rttdev = outputs
                all_outputs.append( (node, manualdestip, outputs) )
                output( ( '%s ' % manualdestip ) if received else 'X ' )
                output( '\n' )
            else:
                for dest in hosts:
                    if node != dest:
                        opts = ''
                        if timeout:
                            opts = '-W %s' % timeout
                        result = node.cmd( 'ping -c1 %s %s' % (opts, dest.IP()) )
                        outputs = self._parsePingFull( result )
                        sent, received, rttmin, rttavg, rttmax, rttdev = outputs
                        all_outputs.append( (node, dest, outputs) )
                        output( ( '%s ' % dest.name ) if received else 'X ' )
        output( "*** Results: \n" )
        for outputs in all_outputs:
            src, dest, ping_outputs = outputs
            sent, received, rttmin, rttavg, rttmax, rttdev = ping_outputs
            output( " %s->%s: %s/%s, " % (src, dest, sent, received ) )
            output( "rtt min/avg/max/mdev %0.3f/%0.3f/%0.3f/%0.3f ms\n" %
                    (rttmin, rttavg, rttmax, rttdev) )
        return all_outputs

    def pingAll( self, timeout=None ):
        """Ping between all hosts.
           returns: ploss packet loss percentage"""
        return self.ping( timeout=timeout )

    def pingPair( self ):
        """Ping between first two hosts, useful for testing.
           returns: ploss packet loss percentage"""
        hosts = [ self.hosts[ 0 ], self.hosts[ 1 ] ]
        return self.ping( hosts=hosts )

    def pingAllFull( self ):
        """Ping between all hosts.
           returns: ploss packet loss percentage"""
        return self.pingFull()

    def pingPairFull( self ):
        """Ping between first two hosts, useful for testing.
           returns: ploss packet loss percentage"""
        hosts = [ self.hosts[ 0 ], self.hosts[ 1 ] ]
        return self.pingFull( hosts=hosts )

    @staticmethod
    def _parseIperf( iperfOutput ):
        """Parse iperf output and return bandwidth.
           iperfOutput: string
           returns: result string"""
        r = r'([\d\.]+ \w+/sec)'
        m = re.findall( r, iperfOutput )
        if m:
            return m[-1]
        else:
            # was: raise Exception(...)
            error( 'could not parse iperf output: ' + iperfOutput )
            return ''

    # XXX This should be cleaned up

    def iperf( self, hosts=None, l4Type='TCP', udpBw='10M', fmt=None,
               seconds=5, port=5001):
        """Run iperf between two hosts.
           hosts: list of hosts; if None, uses first and last hosts
           l4Type: string, one of [ TCP, UDP ]
           udpBw: bandwidth target for UDP test
           fmt: iperf format argument if any
           seconds: iperf time to transmit
           port: iperf port
           returns: two-element array of [ server, client ] speeds
           note: send() is buffered, so client rate can be much higher than
           the actual transmission rate; on an unloaded system, server
           rate should be much closer to the actual receive rate"""
        hosts = hosts or [ self.hosts[ 0 ], self.hosts[ -1 ] ]
        assert len( hosts ) == 2
        client, server = hosts
        output( '*** Iperf: testing', l4Type, 'bandwidth between',
                client, 'and', server, '\n' )
        server.cmd( 'killall -9 iperf' )
        iperfArgs = 'iperf -p %d ' % port
        bwArgs = ''
        if l4Type == 'UDP':
            iperfArgs += '-u '
            bwArgs = '-b ' + udpBw + ' '
        elif l4Type != 'TCP':
            raise Exception( 'Unexpected l4 type: %s' % l4Type )
        if fmt:
            iperfArgs += '-f %s ' % fmt
        server.sendCmd( iperfArgs + '-s' )
        if l4Type == 'TCP':
            if not waitListening( client, server.IP(), port ):
                raise Exception( 'Could not connect to iperf on port %d'
                                 % port )
        cliout = client.cmd( iperfArgs + '-t %d -c ' % seconds +
                             server.IP() + ' ' + bwArgs )
        debug( 'Client output: %s\n' % cliout )
        servout = ''
        # We want the last *b/sec from the iperf server output
        # for TCP, there are two of them because of waitListening
        count = 2 if l4Type == 'TCP' else 1
        while len( re.findall( '/sec', servout ) ) < count:
            servout += server.monitor( timeoutms=5000 )
        server.sendInt()
        servout += server.waitOutput()
        debug( 'Server output: %s\n' % servout )
        result = [ self._parseIperf( servout ), self._parseIperf( cliout ) ]
        if l4Type == 'UDP':
            result.insert( 0, udpBw )
        output( '*** Results: %s\n' % result )
        return result

    def runCpuLimitTest( self, cpu, duration=5 ):
        """run CPU limit test with 'while true' processes.
        cpu: desired CPU fraction of each host
        duration: test duration in seconds (integer)
        returns a single list of measured CPU fractions as floats.
        """
        cores = int( quietRun( 'nproc' ) )
        pct = cpu * 100
        info( '*** Testing CPU %.0f%% bandwidth limit\n' % pct )
        hosts = self.hosts
        cores = int( quietRun( 'nproc' ) )
        # number of processes to run a while loop on per host
        num_procs = int( ceil( cores * cpu ) )
        pids = {}
        for h in hosts:
            pids[ h ] = []
            for _core in range( num_procs ):
                h.cmd( 'while true; do a=1; done &' )
                pids[ h ].append( h.cmd( 'echo $!' ).strip() )
        outputs = {}
        time = {}
        # get the initial cpu time for each host
        for host in hosts:
            outputs[ host ] = []
            with open( '/sys/fs/cgroup/cpuacct/%s/cpuacct.usage' %
                       host, 'r' ) as f:
                time[ host ] = float( f.read() )
        for _ in range( duration ):
            sleep( 1 )
            for host in hosts:
                with open( '/sys/fs/cgroup/cpuacct/%s/cpuacct.usage' %
                           host, 'r' ) as f:
                    readTime = float( f.read() )
                outputs[ host ].append( ( ( readTime - time[ host ] )
                                        / 1000000000 ) / cores * 100 )
                time[ host ] = readTime
        for h, pids in pids.items():
            for pid in pids:
                h.cmd( 'kill -9 %s' % pid )
        cpu_fractions = []
        for _host, outputs in outputs.items():
            for pct in outputs:
                cpu_fractions.append( pct )
        output( '*** Results: %s\n' % cpu_fractions )
        return cpu_fractions

    # BL: I think this can be rewritten now that we have
    # a real link class.
    def configLinkStatus( self, src, dst, status ):
        """Change status of src <-> dst links.
           src: node name
           dst: node name
           status: string {up, down}"""
        if src not in self.nameToNode:
            error( 'src not in network: %s\n' % src )
        elif dst not in self.nameToNode:
            error( 'dst not in network: %s\n' % dst )
        else:
            if isinstance( src, basestring ):
                src = self.nameToNode[ src ]
            if isinstance( dst, basestring ):
                dst = self.nameToNode[ dst ]
            connections = src.connectionsTo( dst )
            if len( connections ) == 0:
                error( 'src and dst not connected: %s %s\n' % ( src, dst) )
            for srcIntf, dstIntf in connections:
                result = srcIntf.ifconfig( status )
                if result:
                    error( 'link src status change failed: %s\n' % result )
                result = dstIntf.ifconfig( status )
                if result:
                    error( 'link dst status change failed: %s\n' % result )

    def interact( self ):
        "Start network and run our simple CLI."
        self.start()
        result = CLI( self )
        self.stop()
        return result

    inited = False

    @classmethod
    def init( cls ):
        "Initialize Mininet"
        if cls.inited:
            return
        ensureRoot()
        fixLimits()
        cls.inited = True

    """
    A Mininet with Docker related methods.
    Inherits Mininet.
    This class is not more than API beautification.
    """

    def addDocker( self, name, cls=Docker, **params ):
        """
        Wrapper for addHost method that adds a
        Docker container as a host.
        """
        return self.addHost( name, cls=cls, **params)

    def removeDocker( self, name, **params):
        """
        Wrapper for removeHost. Just to be complete.
        """
        return self.removeHost(name, **params)

    def addExtSAP(self, sapName, sapIP, dpid=None, **params):
        """
        Add an external Service Access Point, implemented as an OVSBridge
        :param sapName:
        :param sapIP: str format: x.x.x.x/x
        :param dpid:
        :param params:
        :return:
        """
        SAPswitch = self.addSwitch(sapName, cls=OVSBridge, prefix=SAP_PREFIX,
                                   dpid=dpid, ip=sapIP, **params)
        self.SAPswitches[sapName] = SAPswitch

        NAT = params.get('NAT', False)
        if NAT:
            self.addSAPNAT(SAPswitch)

        return SAPswitch

    def removeExtSAP(self, sapName):
        SAPswitch = self.SAPswitches[sapName]
        info( 'stopping external SAP:' + SAPswitch.name + ' \n' )
        SAPswitch.stop()
        SAPswitch.terminate()

        self.removeSAPNAT(SAPswitch)

    def addSAPNAT(self, SAPSwitch):
        """
        Add NAT to the Containernet, so external SAPs can reach the outside internet through the host
        :param SAPSwitch: Instance of the external SAP switch
        :param SAPNet: Subnet of the external SAP as str (eg. '10.10.1.0/30')
        :return:
        """
        SAPip = SAPSwitch.ip
        SAPNet = str(ipaddress.IPv4Network(unicode(SAPip), strict=False))
        # due to a bug with python-iptables, removing and finding rules does not succeed when the mininet CLI is running
        # so we use the iptables tool
        # create NAT rule
        rule0_ = "iptables -t nat -A POSTROUTING ! -o {0} -s {1} -j MASQUERADE".format(SAPSwitch.deployed_name, SAPNet)
        p = Popen(shlex.split(rule0_))
        p.communicate()

        # create FORWARD rule
        rule1_ = "iptables -A FORWARD -o {0} -j ACCEPT".format(SAPSwitch.deployed_name)
        p = Popen(shlex.split(rule1_))
        p.communicate()

        rule2_ = "iptables -A FORWARD -i {0} -j ACCEPT".format(SAPSwitch.deployed_name)
        p = Popen(shlex.split(rule2_))
        p.communicate()

        info("added SAP NAT rules for: {0} - {1}\n".format(SAPSwitch.name, SAPNet))

    def removeSAPNAT(self, SAPSwitch):
        SAPip = SAPSwitch.ip
        SAPNet = str(ipaddress.IPv4Network(unicode(SAPip), strict=False))
        # due to a bug with python-iptables, removing and finding rules does not succeed when the mininet CLI is running
        # so we use the iptables tool
        rule0_ = "iptables -t nat -D POSTROUTING ! -o {0} -s {1} -j MASQUERADE".format(SAPSwitch.deployed_name, SAPNet)
        p = Popen(shlex.split(rule0_))
        p.communicate()

        rule1_ = "iptables -D FORWARD -o {0} -j ACCEPT".format(SAPSwitch.deployed_name)
        p = Popen(shlex.split(rule1_))
        p.communicate()

        rule2_ = "iptables -D FORWARD -i {0} -j ACCEPT".format(SAPSwitch.deployed_name)
        p = Popen(shlex.split(rule2_))
        p.communicate()

        info("remove SAP NAT rules for: {0} - {1}\n".format(SAPSwitch.name, SAPNet))


class MininetWithControlNet( Mininet ):

    """Control network support:

       Create an explicit control network. Currently this is only
       used/usable with the user datapath.

       Notes:

       1. If the controller and switches are in the same (e.g. root)
          namespace, they can just use the loopback connection.

       2. If we can get unix domain sockets to work, we can use them
          instead of an explicit control network.

       3. Instead of routing, we could bridge or use 'in-band' control.

       4. Even if we dispense with this in general, it could still be
          useful for people who wish to simulate a separate control
          network (since real networks may need one!)

       5. Basically nobody ever used this code, so it has been moved
          into its own class.

       6. Ultimately we may wish to extend this to allow us to create a
          control network which every node's control interface is
          attached to."""

    def configureControlNetwork( self ):
        "Configure control network."
        self.configureRoutedControlNetwork()

    # We still need to figure out the right way to pass
    # in the control network location.

    def configureRoutedControlNetwork( self, ip='192.168.123.1',
                                       prefixLen=16 ):
        """Configure a routed control network on controller and switches.
           For use with the user datapath only right now."""
        controller = self.controllers[ 0 ]
        info( controller.name + ' <->' )
        cip = ip
        snum = ipParse( ip )
        for switch in self.switches:
            info( ' ' + switch.name )
            link = self.link( switch, controller, port1=0 )
            sintf, cintf = link.intf1, link.intf2
            switch.controlIntf = sintf
            snum += 1
            while snum & 0xff in [ 0, 255 ]:
                snum += 1
            sip = ipStr( snum )
            cintf.setIP( cip, prefixLen )
            sintf.setIP( sip, prefixLen )
            controller.setHostRoute( sip, cintf )
            switch.setHostRoute( cip, sintf )
        info( '\n' )
        info( '*** Testing control network\n' )
        while not cintf.isUp():
            info( '*** Waiting for', cintf, 'to come up\n' )
            sleep( 1 )
        for switch in self.switches:
            while not sintf.isUp():
                info( '*** Waiting for', sintf, 'to come up\n' )
                sleep( 1 )
            if self.ping( hosts=[ switch, controller ] ) != 0:
                error( '*** Error: control network test failed\n' )
                exit( 1 )
        info( '\n' )
