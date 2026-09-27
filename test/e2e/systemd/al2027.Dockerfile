# Amazon Linux 2027 with systemd as PID 1, for test/e2e/run_systemd.sh --distro=al2027
# (test/e2e/systemd/container.sh). Stands in for an EC2 instance: the same packages,
# systemd, logind and user managers, but no SELinux (containers cannot load a policy)
# and no cloud-init. install.sh installs everything Motuz needs; this image only adds
# what an AMI has and the minimal container image lacks.
FROM public.ecr.aws/amazonlinux/amazonlinux:2027
RUN dnf -y -q install systemd systemd-pam dbus-broker sudo procps-ng iproute passwd \
        hostname findutils util-linux shadow-utils which tar gzip \
    && dnf clean all \
    && systemctl mask systemd-firstboot.service systemd-homed.service systemd-userdbd.socket \
        systemd-networkd-wait-online.service getty@tty1.service console-getty.service 2>/dev/null; \
    systemctl set-default multi-user.target
STOPSIGNAL SIGRTMIN+3
CMD ["/usr/sbin/init"]
