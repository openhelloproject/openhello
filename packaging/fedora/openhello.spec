Name:           openhello
Version:        0.3.0
Release:        1%{?dist}
Summary:        TPM-backed fingerprint and face sign-in for Linux

# MIT: OpenHello and the YuNet model; Apache-2.0: the SFace model
License:        MIT AND Apache-2.0
URL:            https://github.com/openhelloproject/openhello
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  gcc
BuildRequires:  cmake
BuildRequires:  pkgconfig(pam)
BuildRequires:  pkgconfig(libsystemd)
BuildRequires:  pkgconfig(libcrypto)
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  systemd-rpm-macros
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
# %%check imports the daemon's modules, which proves the runtime deps resolve.
BuildRequires:  python3-gobject-base
BuildRequires:  python3-tpm2-pytss
BuildRequires:  python3-cryptography
BuildRequires:  python3-numpy

Requires:       python3-gobject-base
Requires:       python3-tpm2-pytss
Requires:       python3-cryptography
Requires:       python3-numpy
Requires:       fprintd
Requires:       polkit
Requires:       dbus
# IR face sign-in; without them only fingerprint is offered.
Recommends:     python3-opencv
Recommends:     python3-onnxruntime

%description
OpenHello brings Windows Hello–style sign-in to Linux: fingerprint (via
fprintd) and infrared face recognition behind one D-Bus service, with the
sign-in credential sealed in the TPM rather than a plain pass/fail check.
PAM integration is opt-in: run "openhello-pam enable" as root.

%package        setup
Summary:        Enrollment app for OpenHello biometric sign-in
Requires:       %{name} = %{version}-%{release}
Requires:       gtk4
Requires:       libadwaita

%description    setup
GTK4/libadwaita app to register fingerprints and faces for OpenHello sign-in.

%prep
%autosetup

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel
%cmake -S pam -DOPENHELLO_BUILD_HARNESS=OFF -DPAM_MODULE_DIR=%{_libdir}/security
%cmake_build

%install
%pyproject_install
%cmake_install
install -Dm755 data/openhellod                       %{buildroot}%{_libexecdir}/openhellod
install -Dm755 data/pam/openhello-pam                %{buildroot}%{_sbindir}/openhello-pam
install -Dm644 data/systemd/openhellod.service       %{buildroot}%{_unitdir}/openhellod.service
install -Dm644 data/dbus/org.openhello.Daemon1.conf  %{buildroot}%{_datadir}/dbus-1/system.d/org.openhello.Daemon1.conf
install -Dm644 data/dbus/org.openhello.Daemon1.service %{buildroot}%{_datadir}/dbus-1/system-services/org.openhello.Daemon1.service
install -Dm644 src/openhello/daemon/org.openhello.Daemon1.xml %{buildroot}%{_datadir}/dbus-1/interfaces/org.openhello.Daemon1.xml
install -Dm644 data/polkit/org.openhello.policy      %{buildroot}%{_datadir}/polkit-1/actions/org.openhello.policy
install -Dm644 data/org.openhello.Setup.desktop      %{buildroot}%{_datadir}/applications/org.openhello.Setup.desktop
install -Dm644 data/org.openhello.Setup.metainfo.xml %{buildroot}%{_metainfodir}/org.openhello.Setup.metainfo.xml
install -Dm644 data/icons/hicolor/scalable/apps/org.openhello.Setup.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/org.openhello.Setup.svg
install -Dm644 data/icons/hicolor/symbolic/apps/org.openhello.Setup-symbolic.svg %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps/org.openhello.Setup-symbolic.svg
install -Dm644 -t %{buildroot}%{_datadir}/openhello/models models/*.onnx models/LICENSES.md
install -dm700 %{buildroot}%{_sharedstatedir}/openhello

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/org.openhello.Setup.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/org.openhello.Setup.metainfo.xml
%{py3_test_envvars} %{python3} -c "
import openhello.daemon.service, openhello.daemon.orchestrator
import openhello.tpm.keystore, openhello.backends.fingerprint, openhello.backends.face
"

%post
%systemd_post openhellod.service

%preun
%systemd_preun openhellod.service
# Never leave a PAM stack pointing at a module that's about to disappear.
if [ $1 -eq 0 ] && [ -x %{_sbindir}/openhello-pam ]; then
    %{_sbindir}/openhello-pam disable >/dev/null 2>&1 || :
fi

%postun
%systemd_postun_with_restart openhellod.service

%files
%license LICENSE
%doc README.md ARCHITECTURE.md docs/SECURITY.md
%{python3_sitelib}/openhello/
%exclude %{python3_sitelib}/openhello/gui/
%{python3_sitelib}/openhello-%{version}*.dist-info/
%{_libdir}/security/pam_openhello.so
%{_libexecdir}/openhellod
%{_sbindir}/openhello-pam
%{_unitdir}/openhellod.service
%{_datadir}/dbus-1/system.d/org.openhello.Daemon1.conf
%{_datadir}/dbus-1/system-services/org.openhello.Daemon1.service
%{_datadir}/dbus-1/interfaces/org.openhello.Daemon1.xml
%{_datadir}/polkit-1/actions/org.openhello.policy
%{_datadir}/openhello/
%dir %attr(0700,root,root) %{_sharedstatedir}/openhello

%files setup
%{python3_sitelib}/openhello/gui/
%{_bindir}/openhello-setup
%{_datadir}/applications/org.openhello.Setup.desktop
%{_metainfodir}/org.openhello.Setup.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/org.openhello.Setup.svg
%{_datadir}/icons/hicolor/symbolic/apps/org.openhello.Setup-symbolic.svg

%changelog
* Mon Sep 28 2026 Peneal Feleke Aragaw <penealfeleke17pro@gmail.com> - 0.3.0-1
- First public release
