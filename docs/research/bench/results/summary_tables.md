
### raw_constrained.jsonl

| constraint | scenario | n | Gbit/s mean ± sd | min to max | sender cores | receiver cores | system busy cores | cores per Gbit/s |
|---|---|---|---|---|---|---|---|---|
| quota-0.5 | iperf3-3.21 TCP P1 | 3 | 7.12 ± 0.20 | 6.87 to 7.35 | 0.51 ± 0.00 | 0.18 ± 0.01 | 0.76 ± 0.00 | 0.097 ± 0.003 |
| quota-0.5 | iperf3-3.21 TCP P4 | 3 | 13.37 ± 0.42 | 13.06 to 13.96 | 0.48 ± 0.03 | 0.50 ± 0.01 | 1.07 ± 0.04 | 0.073 ± 0.002 |
| quota-0.5 | py blocking sendall/recv_into P1 | 3 | 7.49 ± 0.02 | 7.46 to 7.52 | 0.50 ± 0.00 | 0.18 ± 0.00 | 0.81 ± 0.02 | 0.091 ± 0.000 |
| quota-0.5 | py uvloop sock_sendall/sock_recv_into P1 | 3 | 9.04 ± 0.24 | 8.70 to 9.21 | 0.50 ± 0.00 | 0.33 ± 0.00 | 0.99 ± 0.02 | 0.092 ± 0.003 |
| quota-0.5 | HTTP down srv=aiohttp cli=aiohttp | 3 | 5.85 ± 0.67 | 4.99 to 6.61 | 0.40 ± 0.06 | 0.53 ± 0.00 | 0.99 ± 0.06 | 0.160 ± 0.008 |
| quota-0.5 | HTTP down srv=uvicorn cli=httpx | 3 | 2.17 ± 0.07 | 2.08 to 2.24 | 0.18 ± 0.01 | 0.51 ± 0.00 | 0.79 ± 0.01 | 0.321 ± 0.008 |
| quota-0.5 | HTTP down srv=go cli=go | 3 | 12.61 ± 0.68 | 11.70 to 13.32 | 0.50 ± 0.00 | 0.41 ± 0.01 | 0.91 ± 0.01 | 0.073 ± 0.004 |
| quota-1.0 | iperf3-3.21 TCP P1 | 3 | 13.64 ± 0.31 | 13.40 to 14.08 | 0.98 ± 0.00 | 0.35 ± 0.01 | 1.41 ± 0.01 | 0.098 ± 0.003 |
| quota-1.0 | iperf3-3.21 TCP P4 | 3 | 25.30 ± 2.32 | 22.12 to 27.62 | 0.77 ± 0.17 | 1.00 ± 0.01 | 1.87 ± 0.18 | 0.070 ± 0.000 |
| quota-1.0 | py blocking sendall/recv_into P1 | 3 | 14.96 ± 0.15 | 14.78 to 15.15 | 0.98 ± 0.00 | 0.33 ± 0.00 | 1.40 ± 0.02 | 0.087 ± 0.001 |
| quota-1.0 | py uvloop sock_sendall/sock_recv_into P1 | 3 | 18.51 ± 0.64 | 17.84 to 19.36 | 0.98 ± 0.00 | 0.64 ± 0.01 | 1.74 ± 0.03 | 0.088 ± 0.003 |
| quota-1.0 | HTTP down srv=aiohttp cli=aiohttp | 3 | 12.43 ± 1.18 | 10.78 to 13.45 | 0.87 ± 0.07 | 0.97 ± 0.03 | 1.86 ± 0.03 | 0.149 ± 0.011 |
| quota-1.0 | HTTP down srv=uvicorn cli=httpx | 3 | 4.46 ± 0.11 | 4.31 to 4.55 | 0.35 ± 0.01 | 1.00 ± 0.00 | 1.42 ± 0.01 | 0.302 ± 0.006 |
| quota-1.0 | HTTP down srv=go cli=go | 3 | 29.01 ± 2.51 | 26.90 to 32.53 | 1.00 ± 0.00 | 0.81 ± 0.02 | 1.70 ± 0.03 | 0.063 ± 0.005 |
| pin-shared | iperf3-3.21 TCP P1 | 3 | 14.57 ± 0.47 | 14.18 to 15.24 | 0.17 ± 0.02 | 0.81 ± 0.02 | 1.02 ± 0.00 | 0.067 ± 0.002 |
| pin-shared | iperf3-3.21 TCP P4 | 3 | 14.65 ± 0.15 | 14.47 to 14.84 | 0.64 ± 0.01 | 0.34 ± 0.01 | 1.03 ± 0.00 | 0.067 ± 0.001 |
| pin-shared | py blocking sendall/recv_into P1 | 3 | 15.02 ± 0.52 | 14.63 to 15.76 | 0.49 ± 0.01 | 0.24 ± 0.01 | 1.03 ± 0.00 | 0.048 ± 0.001 |
| pin-shared | py uvloop sock_sendall/sock_recv_into P1 | 3 | 13.85 ± 0.43 | 13.25 to 14.18 | 0.47 ± 0.01 | 0.34 ± 0.02 | 1.03 ± 0.00 | 0.059 ± 0.002 |
| pin-shared | HTTP down srv=aiohttp cli=aiohttp | 3 | 5.90 ± 0.10 | 5.80 to 6.04 | 0.22 ± 0.01 | 0.79 ± 0.01 | 1.03 ± 0.00 | 0.172 ± 0.003 |
| pin-shared | HTTP down srv=uvicorn cli=httpx | 3 | 3.39 ± 0.06 | 3.33 to 3.47 | 0.15 ± 0.00 | 0.86 ± 0.00 | 1.03 ± 0.01 | 0.296 ± 0.006 |
| pin-shared | HTTP down srv=go cli=go | 3 | 15.10 ± 1.42 | 14.09 to 17.11 | 0.51 ± 0.02 | 0.43 ± 0.02 | 1.04 ± 0.02 | 0.063 ± 0.005 |

### raw_extras.jsonl

| constraint | scenario | n | Gbit/s mean ± sd | min to max | sender cores | receiver cores | system busy cores | cores per Gbit/s |
|---|---|---|---|---|---|---|---|---|
| none | py blocking P1 chunk=64KiB | 3 | 10.62 ± 0.16 | 10.42 to 10.81 | 0.98 ± 0.00 | 0.33 ± 0.01 | 1.37 ± 0.01 | 0.124 ± 0.002 |
| none | py blocking P1 chunk=16KiB | 3 | 12.46 ± 1.21 | 10.86 to 13.77 | 0.99 ± 0.00 | 0.64 ± 0.01 | 1.67 ± 0.02 | 0.132 ± 0.012 |
| none | py uvloop sock_* P1 chunk=64KiB | 3 | 7.26 ± 0.19 | 7.09 to 7.53 | 0.98 ± 0.00 | 0.62 ± 0.00 | 1.74 ± 0.04 | 0.220 ± 0.005 |
| none | HTTP down srv=go cli=aiohttp loop=asyncio(stock) | 3 | 12.54 ± 0.53 | 11.97 to 13.24 | 0.70 ± 0.06 | 1.00 ± 0.01 | 1.71 ± 0.04 | 0.136 ± 0.002 |
| none | HTTP down srv=go cli=httpx loop=asyncio(stock) | 3 | 3.68 ± 0.10 | 3.54 to 3.79 | 0.14 ± 0.00 | 1.00 ± 0.00 | 1.21 ± 0.00 | 0.311 ± 0.008 |

### raw_http.jsonl

| constraint | scenario | n | Gbit/s mean ± sd | min to max | sender cores | receiver cores | system busy cores | cores per Gbit/s |
|---|---|---|---|---|---|---|---|---|
| none | HTTP down srv=aiohttp cli=aiohttp | 3 | 12.00 ± 0.20 | 11.79 to 12.27 | 0.83 ± 0.03 | 0.98 ± 0.03 | 1.90 ± 0.05 | 0.151 ± 0.004 |
| none | HTTP down srv=uvicorn cli=httpx | 3 | 4.42 ± 0.05 | 4.36 to 4.47 | 0.34 ± 0.01 | 1.00 ± 0.01 | 1.46 ± 0.03 | 0.303 ± 0.003 |
| none | HTTP down srv=uvicorn cli=aiohttp | 3 | 11.36 ± 0.35 | 11.04 to 11.84 | 0.80 ± 0.04 | 0.99 ± 0.01 | 1.90 ± 0.03 | 0.158 ± 0.004 |
| none | HTTP down srv=go cli=go | 3 | 28.27 ± 1.06 | 26.90 to 29.48 | 1.03 ± 0.01 | 0.81 ± 0.01 | 1.83 ± 0.06 | 0.065 ± 0.002 |
| none | HTTP down srv=go cli=aiohttp | 3 | 9.90 ± 0.88 | 8.68 to 10.71 | 0.53 ± 0.13 | 1.01 ± 0.01 | 1.64 ± 0.15 | 0.155 ± 0.003 |
| none | HTTP down srv=go cli=httpx | 3 | 4.41 ± 0.09 | 4.28 to 4.48 | 0.24 ± 0.01 | 1.00 ± 0.00 | 1.35 ± 0.01 | 0.282 ± 0.006 |
| none | HTTP down srv=aiohttp cli=go | 3 | 13.56 ± 0.10 | 13.44 to 13.68 | 0.97 ± 0.00 | 0.62 ± 0.01 | 1.58 ± 0.04 | 0.117 ± 0.001 |
| none | HTTP down srv=uvicorn cli=go | 3 | 13.39 ± 0.13 | 13.23 to 13.53 | 0.97 ± 0.00 | 0.63 ± 0.01 | 1.55 ± 0.01 | 0.119 ± 0.002 |
| none | HTTP up srv=aiohttp cli=aiohttp | 3 | 12.24 ± 0.51 | 11.56 to 12.80 | 0.89 ± 0.01 | 0.95 ± 0.03 | 1.88 ± 0.03 | 0.150 ± 0.006 |
| none | HTTP up srv=uvicorn cli=httpx | 3 | 5.80 ± 0.03 | 5.77 to 5.82 | 0.39 ± 0.01 | 0.99 ± 0.00 | 1.48 ± 0.02 | 0.237 ± 0.001 |
| none | HTTP up srv=uvicorn cli=aiohttp | 3 | 5.84 ± 0.10 | 5.69 to 5.93 | 0.39 ± 0.01 | 0.99 ± 0.00 | 1.47 ± 0.03 | 0.236 ± 0.005 |
| none | HTTP up srv=go cli=go | 3 | 25.64 ± 1.85 | 23.23 to 27.74 | 1.05 ± 0.00 | 0.84 ± 0.01 | 1.78 ± 0.04 | 0.074 ± 0.006 |
| none | HTTP up srv=go cli=aiohttp | 3 | 13.37 ± 0.16 | 13.14 to 13.49 | 0.99 ± 0.00 | 0.62 ± 0.01 | 1.54 ± 0.01 | 0.121 ± 0.000 |
| none | HTTP up srv=go cli=httpx | 3 | 12.20 ± 0.20 | 12.01 to 12.48 | 0.99 ± 0.01 | 0.59 ± 0.01 | 1.50 ± 0.01 | 0.129 ± 0.001 |
| none | HTTP up srv=aiohttp cli=go | 3 | 11.48 ± 0.73 | 10.84 to 12.50 | 0.67 ± 0.04 | 0.98 ± 0.01 | 1.73 ± 0.05 | 0.144 ± 0.006 |
| none | HTTP up srv=uvicorn cli=go | 3 | 5.89 ± 0.26 | 5.60 to 6.23 | 0.24 ± 0.02 | 0.99 ± 0.00 | 1.33 ± 0.03 | 0.210 ± 0.006 |

### raw_tcp.jsonl

| constraint | scenario | n | Gbit/s mean ± sd | min to max | sender cores | receiver cores | system busy cores | cores per Gbit/s |
|---|---|---|---|---|---|---|---|---|
| none | iperf3-3.21 TCP P1 | 3 | 13.66 ± 0.17 | 13.51 to 13.90 | 0.97 ± 0.01 | 0.36 ± 0.00 | 1.47 ± 0.06 | 0.097 ± 0.001 |
| none | iperf3-3.21 TCP P4 | 3 | 56.78 ± 0.71 | 56.19 to 57.78 | 1.68 ± 0.10 | 2.04 ± 0.09 | 3.98 ± 0.00 | 0.066 ± 0.000 |
| none | iperf3-3.16(apt) TCP P1 | 3 | 12.06 ± 0.10 | 11.93 to 12.15 | 0.98 ± 0.00 | 0.36 ± 0.00 | 1.53 ± 0.05 | 0.111 ± 0.001 |
| none | iperf3-3.16(apt) TCP P4 | 3 | 55.12 ± 1.03 | 53.74 to 56.22 | 1.54 ± 0.16 | 2.20 ± 0.15 | 3.98 ± 0.01 | 0.068 ± 0.001 |
| none | py blocking sendall/recv_into P1 | 3 | 14.69 ± 0.06 | 14.65 to 14.77 | 0.96 ± 0.01 | 0.34 ± 0.00 | 1.55 ± 0.11 | 0.088 ± 0.001 |
| none | py blocking sendall/recv_into P4 (threads) | 3 | 53.22 ± 1.07 | 51.76 to 54.28 | 1.74 ± 0.03 | 1.28 ± 0.04 | 3.78 ± 0.04 | 0.057 ± 0.001 |
| none | py os.sendfile/recv_into P1 | 3 | 11.77 ± 0.58 | 11.06 to 12.48 | 0.96 ± 0.01 | 0.46 ± 0.01 | 1.58 ± 0.03 | 0.121 ± 0.005 |
| none | py asyncio sock_sendall/sock_recv_into P1 | 3 | 22.25 ± 0.34 | 21.81 to 22.62 | 0.97 ± 0.01 | 0.73 ± 0.00 | 1.90 ± 0.03 | 0.076 ± 0.001 |
| none | py uvloop sock_sendall/sock_recv_into P1 | 3 | 18.80 ± 0.41 | 18.22 to 19.15 | 0.96 ± 0.01 | 0.65 ± 0.01 | 1.80 ± 0.02 | 0.086 ± 0.001 |
| none | py uvloop Transport/BufferedProtocol P1 | 3 | 14.79 ± 0.03 | 14.76 to 14.83 | 0.96 ± 0.00 | 0.37 ± 0.00 | 1.46 ± 0.02 | 0.090 ± 0.000 |
| none | go net.Conn TCP P1 | 3 | 16.46 ± 0.00 | 16.46 to 16.47 | 1.05 ± 0.01 | 0.64 ± 0.01 | 1.63 ± 0.02 | 0.102 ± 0.000 |
| none | go net.Conn TCP P4 | 3 | 49.01 ± 0.84 | 48.22 to 50.17 | 1.75 ± 0.03 | 1.43 ± 0.03 | 3.43 ± 0.07 | 0.065 ± 0.002 |

### raw_tls.jsonl

| constraint | scenario | n | Gbit/s mean ± sd | min to max | sender cores | receiver cores | system busy cores | cores per Gbit/s |
|---|---|---|---|---|---|---|---|---|
| none | HTTPS down srv=go cli=go | 3 | 10.68 ± 0.43 | 10.14 to 11.19 | 1.03 ± 0.00 | 0.98 ± 0.01 | 1.99 ± 0.02 | 0.188 ± 0.008 |
| none | HTTPS down srv=go cli=aiohttp | 3 | 4.74 ± 0.12 | 4.65 to 4.91 | 0.50 ± 0.02 | 1.02 ± 0.01 | 1.51 ± 0.03 | 0.319 ± 0.009 |
| none | HTTPS down srv=go cli=httpx | 3 | 2.16 ± 0.03 | 2.12 to 2.19 | 0.25 ± 0.01 | 1.00 ± 0.00 | 1.30 ± 0.03 | 0.579 ± 0.008 |
| none | HTTPS down srv=aiohttp cli=aiohttp | 3 | 4.43 ± 0.14 | 4.24 to 4.57 | 0.69 ± 0.19 | 0.97 ± 0.03 | 1.71 ± 0.14 | 0.373 ± 0.031 |
| none | HTTPS down srv=aiohttp cli=go | 3 | 8.52 ± 2.18 | 5.44 to 10.12 | 0.99 ± 0.01 | 0.67 ± 0.14 | 1.61 ± 0.13 | 0.205 ± 0.044 |
| none | HTTPS down srv=uvicorn cli=go | 3 | 8.35 ± 1.99 | 5.57 to 10.10 | 0.98 ± 0.01 | 0.65 ± 0.16 | 1.62 ± 0.15 | 0.204 ± 0.035 |

### docker_check.jsonl (docker run --cpus=X for BOTH server and client containers)

| docker --cpus | scenario | n | Gbit/s mean ± sd | min to max |
|---|---|---|---|---|
| 0.5 | py blocking sendall/recv_into P1 | 3 | 7.53 ± 0.20 | 7.25 to 7.68 |
| 0.5 | HTTP down srv=aiohttp cli=aiohttp | 3 | 5.43 ± 0.56 | 4.89 to 6.20 |
| 1.0 | py blocking sendall/recv_into P1 | 3 | 14.68 ± 0.28 | 14.48 to 15.07 |
| 1.0 | HTTP down srv=aiohttp cli=aiohttp | 3 | 12.87 ± 0.09 | 12.80 to 13.00 |
| 0.5 | iperf3-3.21 TCP P1 | 3 | 6.64 ± 0.05 | 6.57 to 6.68 |
| 1.0 | iperf3-3.21 TCP P1 | 3 | 13.35 ± 0.40 | 12.85 to 13.83 |
