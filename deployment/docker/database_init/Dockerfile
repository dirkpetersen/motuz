# Build as
# docker build --no-cache=true -t fredhutch/motuz_database_init:latest -f deployment/docker/database_init/Dockerfile .
# docker run -it --rm -v /docker/volumes/postgres:/var/lib/postgresql/data fredhutch/motuz_database_init:latest

FROM postgres:18-alpine

# postgres 18 images default to /var/lib/postgresql/<major>/docker
ENV PGDATA=/var/lib/postgresql/data

COPY \
    ./deployment/docker/database_init/database_init-entrypoint.sh \
    ./deployment/docker/database_init/pg_hba.conf \
    ./deployment/docker/wait-for-it.sh \
    ./deployment/docker/load-secrets.sh \
    /app/

WORKDIR /app
CMD /app/database_init-entrypoint.sh
