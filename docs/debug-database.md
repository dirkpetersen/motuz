# Debugging the Database

Terminal 1

```bash
docker run -it --net='host' postgres:18-alpine
```

Terminal 2

```bash
docker run -it --net='host' postgres:18-alpine /bin/bash
psql -h 0.0.0.0 -U postgres -d postgres
```
