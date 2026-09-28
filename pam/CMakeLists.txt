cmake_minimum_required(VERSION 3.16)
project(pam_openhello C)

include(GNUInstallDirs)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
add_compile_options(-Wall -Wextra -Werror -fPIC)

# Fedora/Arch: /usr/lib64/security or /usr/lib/security.
# Debian/Ubuntu: /usr/lib/<multiarch>/security (GNUInstallDirs picks the libdir).
set(PAM_MODULE_DIR "${CMAKE_INSTALL_FULL_LIBDIR}/security"
    CACHE PATH "Directory PAM modules are installed into")
option(OPENHELLO_BUILD_HARNESS "Build the pam_start_confdir() test harness" ON)

find_library(PAM_LIB pam REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(SDBUS REQUIRED IMPORTED_TARGET libsystemd)
pkg_check_modules(LIBCRYPTO REQUIRED IMPORTED_TARGET libcrypto)

add_library(pam_openhello MODULE pam_openhello.c)
set_target_properties(pam_openhello PROPERTIES PREFIX "" C_VISIBILITY_PRESET hidden)
target_link_libraries(pam_openhello ${PAM_LIB} PkgConfig::SDBUS PkgConfig::LIBCRYPTO)
install(TARGETS pam_openhello LIBRARY DESTINATION "${PAM_MODULE_DIR}")

if(OPENHELLO_BUILD_HARNESS)
  # Runs the module via pam_start_confdir() with a private config dir, so the
  # whole PAM path is testable without root or touching /etc/pam.d.
  # See tests/test_pam_e2e.py. Never installed.
  add_executable(pam_openhello_harness tests/harness.c)
  target_link_libraries(pam_openhello_harness ${PAM_LIB})
endif()
