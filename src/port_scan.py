#!/usr/bin/pyro

#         __________________________
#         |____C_O_P_Y_R_I_G_H_T___|
#         |                        |
#         |  (c) NIWA, 2008-2010   |
#         | Contact: Hilary Oliver |
#         |  h.oliver@niwa.co.nz   |
#         |    +64-4-386 0461      |
#         |________________________|

import Pyro.errors
from cylc_pyro_client import port_interrogator
from cylc_pyro_server import pyro_base_port, pyro_port_range

def get_port( suite, owner, host ):
    found = False
    port = 0
    for p in range( pyro_base_port, pyro_base_port + pyro_port_range ):
        try:
            one, two = port_interrogator( host, p ).interrogate()
        except Pyro.errors.ProtocolError:
            pass
        else:
            if one == suite and two == owner:
                found = True
                port = p
                break
    return ( found, port )    

def check_port( suite, owner, host, port ):
    # is suite,owner running at host:port?
    try:
        one, two = port_interrogator( host, port ).interrogate() 
    except Pyro.errors.ProtocolError:
        # no cylc suite at port
        return False
    else:
        if one == suite and two == owner:
            return True
        else:
            return False
 
def scan( host ):
    suites = []
    for port in range( pyro_base_port, pyro_base_port + pyro_port_range ):
        try:
            suite, owner = port_interrogator( host, port ).interrogate()
        except Pyro.errors.ProtocolError:
            # no suite at this port
            pass
        else:
            suites.append( ( suite, owner, port ) )

    return suites
