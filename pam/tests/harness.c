/* Minimal PAM driver for tests: harness <confdir> <service> <user>
 * Prints PAM messages to stderr and exits with the pam_authenticate code. */
#include <security/pam_appl.h>
#include <stdio.h>
#include <stdlib.h>

static int conv(int n, const struct pam_message **msg, struct pam_response **resp, void *data) {
    (void)data;
    *resp = calloc((size_t)n, sizeof(struct pam_response));
    if (!*resp) return PAM_BUF_ERR;
    for (int i = 0; i < n; i++)
        fprintf(stderr, "[pam msg %d] %s\n", msg[i]->msg_style, msg[i]->msg);
    return PAM_SUCCESS;
}

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s confdir service user\n", argv[0]);
        return 2;
    }
    struct pam_conv c = { conv, NULL };
    pam_handle_t *pamh = NULL;
    int rc = pam_start_confdir(argv[2], argv[3], &c, argv[1], &pamh);
    if (rc != PAM_SUCCESS) return 100 + rc;
    rc = pam_authenticate(pamh, 0);
    printf("%d %s\n", rc, pam_strerror(pamh, rc));
    pam_end(pamh, rc);
    return rc;
}
