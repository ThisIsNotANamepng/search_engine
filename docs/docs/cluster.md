# Cluster

We run a cluster of Linux machines which run docker container (via containerd) crawlers

## Workers

### OS Details

- RockyOS 9.7
- All `in` nodes are air-gapped and can't reach the internet

### Worker Control Server Config

Contained in `etc/stultus/crawler.env`

``` bash
HEAD_URL=http://db01:8000
CRAWLER_IMAGE=db01:5000/stultus:<version>
CTR_PLAIN_HTTP=1
CTR_NAMESPACE=stultus
DATABASE_URL=<postgres_url>
SCRAPER_PROXY=http://db01:8888
SCRAPER_PROXY_PASSWORD=<scraper_password>
WEB_TEXT_STORAGE_SERVER_ADDRESS=http://db01:8003
INITIAL_DESIRED=1
```

### Worker Pull Containers

```
CT="sudo ctr --address /run/k3s/containerd/containerd.sock --namespace homegrown"

$CT images pull --plain-http db01:5000/stultus-control:1.0.0
```

### Worker Run Containers

```
sudo ctr -n stultus run --rm --net-host \
  --mount type=bind,src=/run/k3s/containerd/containerd.sock,dst=/run/k3s/containerd/containerd.sock,options=rbind:rw \
  --mount type=bind,src=$(readlink -f /usr/local/bin/ctr),dst=/usr/local/bin/ctr,options=rbind:ro \
  --env-file /etc/stultus/crawler.env \
  db01:5000/stultus-control:1.0.0 stultus-control
```