# Relaysimulator
some server client setup to simulate somehow in realtime  a bigger relay punch events. 
this repo contains also some tools for reverse engineer the relay data to import to navisport. May contain errors.  

Fetch data for simulation from: 
 - venlat https://results.jukola.com/tulokset/results_j2025_ve_iof.xml
 - jukola https://results.jukola.com/tulokset/results_j2025_ju_iof.xml

# create python virtualenv
`python3 -m venv jukolasim_venv`

activate python3 virtualenv
`source juoklasim_venv/bin/activate`

add dependecies to venv
`pip3 install aiohttp`

start server side ( not real server but something to receive simulator data ) 
`python3 server_ws.py`

start simulator with 4x speed with venla data. 
`python3  simulator.py  --iof results_j2025_ve_iof.xml --speed 4 `





