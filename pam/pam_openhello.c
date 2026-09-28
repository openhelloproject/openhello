/*
 * pam_openhello.c — PAM module that authenticates by asking openhellod to run
 * a live biometric match and unseal a TPM-backed credential, then
 * independently verifying the daemon's answer.
 *
 * The daemon's ok=true is never trusted on its own. PAM sends a fresh random
 * nonce over D-Bus (org.openhello.Daemon1.Authenticate); the daemon must
 * return an Ed25519 signature over it made with a key derived from the
 * TPM-unsealed secret, which PAM verifies against the public key written at
 * enrollment (<state_dir>/<user>/auth.pub). See src/openhello/core/authtoken.py
 * for the other half — build_message() must stay byte-identical to it.
 *
 * IMPORTANT: always deploy this as `sufficient`, never `required`, so a
 * password fallback always exists. See docs/SECURITY.md.
 *
 * Module arguments (all optional):
 *   state_dir=PATH  enrollment data (default /var/lib/openhello)
 *   timeout=SECS    max wait for the daemon's answer (default 30)
 *   quiet           don't print the "scan your fingerprint" prompt
 *
 * The bus is the system bus. sd-bus reads DBUS_SYSTEM_BUS_ADDRESS with
 * secure_getenv(), so inside setuid programs (sudo, su) a user can't point
 * this module at a bus of their choosing; the test suite uses that variable
 * to run against a private bus.
 */

#define PAM_SM_AUTH
#define _GNU_SOURCE

#include <security/pam_appl.h>
#include <security/pam_ext.h>
#include <security/pam_modules.h>

#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <systemd/sd-bus.h>

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <syslog.h>
#include <unistd.h>

#define DEFAULT_STATE_DIR   "/var/lib/openhello"
#define DEFAULT_TIMEOUT_S   30   /* > openhellod's fingerprint VERIFY_TIMEOUT_S */

#define DBUS_NAME        "org.openhello.Daemon1"
#define DBUS_PATH        "/org/openhello/Daemon1"
#define DBUS_IFACE       "org.openhello.Daemon1"

#define AUTH_CONTEXT     "openhello-auth-v1"  /* + NUL, see core/authtoken.py */
#define NONCE_LEN        32
#define PUBKEY_LEN       32
#define SIG_LEN          64
#define MAX_USERNAME     32
#define MAX_SERVICE      255

/* Built with -fvisibility=hidden; only the PAM entry points are exported. */
#define OPENHELLO_EXPORT __attribute__((visibility("default")))

struct options {
    const char *state_dir;
    int timeout_s;
    bool quiet;
};

static void parse_args(pam_handle_t *pamh, int argc, const char **argv, struct options *opt) {
    opt->state_dir = DEFAULT_STATE_DIR;
    opt->timeout_s = DEFAULT_TIMEOUT_S;
    opt->quiet = false;
    for (int i = 0; i < argc; i++) {
        if (strncmp(argv[i], "state_dir=", 10) == 0) {
            opt->state_dir = argv[i] + 10;
        } else if (strncmp(argv[i], "timeout=", 8) == 0) {
            int t = atoi(argv[i] + 8);
            if (t > 0 && t <= 300) opt->timeout_s = t;
        } else if (strcmp(argv[i], "quiet") == 0) {
            opt->quiet = true;
        } else {
            pam_syslog(pamh, LOG_WARNING, "openhello: unknown option %s", argv[i]);
        }
    }
}

/* Same rule as src/openhello/core/users.py: [A-Za-z_][A-Za-z0-9_.-]{0,31}.
 * Also keeps the name safe to use as a path component. */
static bool valid_username(const char *u) {
    size_t n = strlen(u);
    if (n == 0 || n > MAX_USERNAME) return false;
    if (!((u[0] >= 'A' && u[0] <= 'Z') || (u[0] >= 'a' && u[0] <= 'z') || u[0] == '_'))
        return false;
    for (size_t i = 1; i < n; i++) {
        char c = u[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
              c == '_' || c == '.' || c == '-'))
            return false;
    }
    return true;
}

/* The public key decides who can log in, so only trust it if nobody but
 * root (or, for non-root test harnesses, our own euid) could have written
 * it or swapped the directory it lives in. */
static bool trusted_owner(const struct stat *st) {
    return (st->st_uid == 0 || st->st_uid == geteuid()) && !(st->st_mode & (S_IWGRP | S_IWOTH));
}

static int load_pubkey(pam_handle_t *pamh, const struct options *opt, const char *user,
                       unsigned char pub[PUBKEY_LEN]) {
    char dir[512], path[512];
    struct stat st;
    if (snprintf(dir, sizeof(dir), "%s/%s", opt->state_dir, user) >= (int)sizeof(dir) ||
        snprintf(path, sizeof(path), "%s/auth.pub", dir) >= (int)sizeof(path))
        return PAM_AUTHINFO_UNAVAIL;

    if (lstat(dir, &st) != 0) return PAM_AUTHINFO_UNAVAIL; /* not enrolled */
    if (!S_ISDIR(st.st_mode) || !trusted_owner(&st)) {
        pam_syslog(pamh, LOG_ERR, "openhello: refusing untrusted directory %s", dir);
        return PAM_AUTHINFO_UNAVAIL;
    }

    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return PAM_AUTHINFO_UNAVAIL;
    int rc = PAM_AUTHINFO_UNAVAIL;
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || !trusted_owner(&st) ||
        st.st_size != PUBKEY_LEN) {
        pam_syslog(pamh, LOG_ERR, "openhello: refusing untrusted/invalid %s", path);
    } else if (read(fd, pub, PUBKEY_LEN) == PUBKEY_LEN) {
        rc = PAM_SUCCESS;
    }
    close(fd);
    return rc;
}

/* AUTH_CONTEXT NUL || nonce || user || NUL || service — see authtoken.py */
static size_t build_message(unsigned char *buf, size_t cap, const unsigned char nonce[NONCE_LEN],
                            const char *user, const char *service) {
    size_t ctx_len = sizeof(AUTH_CONTEXT); /* includes the NUL */
    size_t ulen = strlen(user), slen = strlen(service);
    size_t need = ctx_len + NONCE_LEN + ulen + 1 + slen;
    if (need > cap) return 0;
    unsigned char *p = buf;
    memcpy(p, AUTH_CONTEXT, ctx_len); p += ctx_len;
    memcpy(p, nonce, NONCE_LEN);      p += NONCE_LEN;
    memcpy(p, user, ulen);            p += ulen;
    *p++ = '\0';
    memcpy(p, service, slen);
    return need;
}

static bool verify_signature(const unsigned char pub[PUBKEY_LEN], const unsigned char sig[SIG_LEN],
                             const unsigned char *msg, size_t msg_len) {
    bool ok = false;
    EVP_PKEY *pkey = EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519, NULL, pub, PUBKEY_LEN);
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    if (pkey && ctx && EVP_DigestVerifyInit(ctx, NULL, NULL, NULL, pkey) == 1)
        ok = EVP_DigestVerify(ctx, sig, SIG_LEN, msg, msg_len) == 1;
    EVP_MD_CTX_free(ctx);
    EVP_PKEY_free(pkey);
    return ok;
}


/* Which sign-in methods the daemon will run for `user` (GetAuthMethods). */
struct methods {
    bool fingerprint;
    bool face;
    int count;
};

static int get_auth_methods(pam_handle_t *pamh, sd_bus *bus, const char *user,
                            struct methods *out) {
    sd_bus_message *reply = NULL;
    sd_bus_error error = SD_BUS_ERROR_NULL;
    int rc = PAM_AUTHINFO_UNAVAIL;
    memset(out, 0, sizeof(*out));

    int r = sd_bus_call_method(bus, DBUS_NAME, DBUS_PATH, DBUS_IFACE, "GetAuthMethods",
                               &error, &reply, "s", user);
    if (r < 0) {
        pam_syslog(pamh, LOG_NOTICE, "openhello: daemon not reachable: %s",
                   error.message ? error.message : strerror(-r));
        goto out;
    }
    if (sd_bus_message_enter_container(reply, 'a', "s") < 0) goto malformed;
    const char *name;
    while ((r = sd_bus_message_read(reply, "s", &name)) > 0) {
        if (strcmp(name, "fingerprint") == 0) out->fingerprint = true;
        else if (strcmp(name, "face") == 0) out->face = true;
        out->count++;
    }
    if (r < 0) goto malformed;
    rc = PAM_SUCCESS;
    goto out;
malformed:
    pam_syslog(pamh, LOG_ERR, "openhello: malformed GetAuthMethods reply");
out:
    sd_bus_error_free(&error);
    sd_bus_message_unref(reply);
    return rc;
}

static const char *prompt_for(const struct methods *m) {
    if (m->fingerprint && m->face) return "Scan your fingerprint or look at the camera";
    if (m->face) return "Look at the camera";
    if (m->fingerprint) return "Scan your fingerprint";
    return "Use your fingerprint or face";
}

/*
 * Calls Authenticate(user, nonce, service) -> (ok, signature, reason).
 * Returns PAM_SUCCESS with *sig filled on ok=true, PAM_AUTH_ERR when the
 * daemon declined, PAM_AUTHINFO_UNAVAIL when it couldn't be asked.
 */
static int call_authenticate(pam_handle_t *pamh, sd_bus *bus, const struct options *opt,
                             const char *user, const unsigned char nonce[NONCE_LEN],
                             const char *service, unsigned char sig[SIG_LEN]) {
    sd_bus_message *msg = NULL, *reply = NULL;
    sd_bus_error error = SD_BUS_ERROR_NULL;
    int rc = PAM_AUTHINFO_UNAVAIL;

    int r = sd_bus_message_new_method_call(bus, &msg, DBUS_NAME, DBUS_PATH, DBUS_IFACE,
                                           "Authenticate");
    if (r >= 0) r = sd_bus_message_append(msg, "s", user);
    if (r >= 0) r = sd_bus_message_append_array(msg, 'y', nonce, NONCE_LEN);
    if (r >= 0) r = sd_bus_message_append(msg, "s", service);
    if (r < 0) {
        rc = PAM_BUF_ERR;
        goto out;
    }

    r = sd_bus_call(bus, msg, (uint64_t)opt->timeout_s * 1000000ULL, &error, &reply);
    if (r < 0) {
        pam_syslog(pamh, LOG_NOTICE, "openhello: daemon call failed: %s",
                   error.message ? error.message : strerror(-r));
        goto out;
    }

    int ok = 0;
    const void *sig_data = NULL;
    size_t sig_len = 0;
    const char *reason = NULL;
    if (sd_bus_message_read(reply, "b", &ok) < 0 ||
        sd_bus_message_read_array(reply, 'y', &sig_data, &sig_len) < 0 ||
        sd_bus_message_read(reply, "s", &reason) < 0) {
        pam_syslog(pamh, LOG_ERR, "openhello: malformed daemon reply");
        goto out;
    }
    if (!ok) {
        pam_syslog(pamh, LOG_NOTICE, "openhello: daemon declined for %s: %s", user,
                   reason ? reason : "");
        rc = PAM_AUTH_ERR;
    } else if (sig_len != SIG_LEN) {
        pam_syslog(pamh, LOG_ERR, "openhello: daemon said ok without a valid signature — rejecting");
        rc = PAM_AUTH_ERR;
    } else {
        memcpy(sig, sig_data, SIG_LEN);
        rc = PAM_SUCCESS;
    }

out:
    sd_bus_error_free(&error);
    sd_bus_message_unref(reply);
    sd_bus_message_unref(msg);
    return rc;
}

static int authenticate_via_daemon(pam_handle_t *pamh, const struct options *opt,
                                   const char *user, const char *service) {
    unsigned char pub[PUBKEY_LEN];
    int rc = load_pubkey(pamh, opt, user, pub);
    if (rc != PAM_SUCCESS) return rc;   /* not enrolled: don't even wake the daemon */

    /* A private connection (not sd_bus_default_system): PAM modules are
     * loaded into long-lived processes like GDM, and we must not share
     * state with the host application. */
    sd_bus *bus = NULL;
    int r = sd_bus_open_system(&bus);
    if (r < 0) {
        pam_syslog(pamh, LOG_ERR, "openhello: cannot connect to system bus: %s", strerror(-r));
        return PAM_AUTHINFO_UNAVAIL;
    }

    struct methods methods;
    rc = get_auth_methods(pamh, bus, user, &methods);
    if (rc == PAM_SUCCESS && methods.count == 0) {
        /* Nothing can sign this user in right now (all methods switched off,
         * sensor missing, face not ready): step aside at once, no prompt. */
        rc = PAM_AUTHINFO_UNAVAIL;
    }
    if (rc != PAM_SUCCESS) goto out;

    if (!opt->quiet) {
        char prompt[128];
        snprintf(prompt, sizeof(prompt), "OpenHello: %s (password prompt follows on timeout)",
                 prompt_for(&methods));
        pam_info(pamh, "%s", prompt);
    }

    unsigned char nonce[NONCE_LEN];
    if (getrandom(nonce, sizeof(nonce), 0) != (ssize_t)sizeof(nonce)) {
        rc = PAM_AUTHINFO_UNAVAIL;
        goto out;
    }
    unsigned char sig[SIG_LEN];
    rc = call_authenticate(pamh, bus, opt, user, nonce, service, sig);
    if (rc == PAM_SUCCESS) {
        unsigned char msg[sizeof(AUTH_CONTEXT) + NONCE_LEN + MAX_USERNAME + 1 + MAX_SERVICE];
        size_t msg_len = build_message(msg, sizeof(msg), nonce, user, service);
        if (msg_len == 0 || !verify_signature(pub, sig, msg, msg_len)) {
            pam_syslog(pamh, LOG_ERR, "openhello: signature verification FAILED for %s", user);
            rc = PAM_AUTH_ERR;
        }
        OPENSSL_cleanse(sig, sizeof(sig));
    }
out:
    sd_bus_flush_close_unref(bus);
    return rc;
}

OPENHELLO_EXPORT PAM_EXTERN int pam_sm_authenticate(pam_handle_t *pamh, int flags,
                                   int argc, const char **argv) {
    (void)flags;
    struct options opt;
    parse_args(pamh, argc, argv, &opt);

    const char *username = NULL;
    if (pam_get_user(pamh, &username, NULL) != PAM_SUCCESS || username == NULL)
        return PAM_AUTHINFO_UNAVAIL;
    if (!valid_username(username)) return PAM_USER_UNKNOWN;

    const void *service_item = NULL;
    const char *service = "";
    if (pam_get_item(pamh, PAM_SERVICE, &service_item) == PAM_SUCCESS && service_item)
        service = service_item;
    if (strlen(service) > MAX_SERVICE) return PAM_AUTHINFO_UNAVAIL;

    int result = authenticate_via_daemon(pamh, &opt, username, service);
    if (result == PAM_SUCCESS)
        pam_syslog(pamh, LOG_INFO, "openhello: authenticated %s (service %s)", username, service);
    else
        pam_syslog(pamh, LOG_NOTICE, "openhello: auth failed for %s (code %d: %s)",
                   username, result, pam_strerror(pamh, result));
    return result;
}

OPENHELLO_EXPORT PAM_EXTERN int pam_sm_setcred(pam_handle_t *pamh, int flags,
                              int argc, const char **argv) {
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_SUCCESS;
}
