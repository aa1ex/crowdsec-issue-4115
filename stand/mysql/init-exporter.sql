-- mysqld_exporter user (used only when the observability profile is up).
-- MAX_USER_CONNECTIONS=3 caps the exporter's own pool footprint so it cannot
-- perturb the pool-pressure metric we are measuring.
CREATE USER 'exporter'@'%' IDENTIFIED BY 'expass'
  WITH MAX_USER_CONNECTIONS 3;
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'%';
FLUSH PRIVILEGES;
