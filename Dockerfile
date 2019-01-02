FROM centos:centos7
MAINTAINER kouhei <kouhei@cse.jp>

RUN yum update -y
RUN yum install gcc wget -y
RUN yum install squid -y
RUN yum install epel-release -y
RUN rpm -Uvh http://rpms.famillecollet.com/enterprise/remi-release-7.rpm
RUN yum install --enablerepo=remi,remi-php72 php php-devel php-mbstring -y
RUN wget "http://delegate.hpcc.jp/anonftp/DeleGate/bin/linux/latest/linux2.6-dg9_9_13.tar.gz"
RUN tar xf linux2.6-dg9_9_13.tar.gz
RUN mv ./dg9_9_13/DGROOT/bin/dg9_9_13 /usr/local/bin/delegate
RUN useradd delegate
WORKDIR /home/delegate
RUN cp /etc/squid/squid.conf /home/delegate/squid.conf
RUN chown delegate:delegate /home/delegate/squid.conf
RUN sed -i '1s/^/acl localnet src 127.0.0.1\/32\n/' /home/delegate/squid.conf
ADD start.php ./
RUN chmod +x ./start.php
RUN chown -R delegate:delegate /var/log/squid/
RUN chown delegate:delegate ./start.php
USER delegate
RUN mkdir config
ADD delegateBase.conf ./
ADD proxyList.txt ./
ADD anonsquid.conf ./
ADD Allowed_IP.txt ./
ADD acl.conf ./
ENTRYPOINT ["/home/delegate/start.php"]
EXPOSE 3128