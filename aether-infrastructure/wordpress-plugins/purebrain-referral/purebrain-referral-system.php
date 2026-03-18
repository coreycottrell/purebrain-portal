<?php
/**
 * Plugin Name: PureBrain Referral System
 * Description: Referral tracking, dashboard API, link generation, payment integration, and reward ledger for PureBrain members.
 * Version:     2.1.0
 * Author:      Aether / Pure Technology
 */

if ( ! defined( 'ABSPATH' ) ) exit;

// ============================================================
// CONSTANTS
// ============================================================
define( 'PB_REFERRAL_VERSION', '2.1.0' );
define( 'PB_REFERRAL_TABLE',          'pb_referrals' );
define( 'PB_REFERRAL_USERS_TABLE',    'pb_referral_users' );
define( 'PB_REFERRAL_LEDGER_TABLE',   'pb_reward_ledger' );
define( 'PB_REFERRAL_REVENUE_SHARE',  0.05 );   // 5%
define( 'PB_REFERRAL_BASE_CREDIT',    5.00 );   // $5 per referral
define( 'PB_REFERRAL_BONUS_THRESHOLD', 5 );     // 5+ referrals
define( 'PB_REFERRAL_BONUS_AMOUNT',   10.00 );  // $10 bonus
define( 'PB_REFERRAL_REWARD_DAYS',    7 );       // days active before reward
define( 'PB_PAYOUT_REQUESTS_FILE',   '/home/jared/purebrain_portal/payout-requests.jsonl' );
define( 'PB_PAYOUT_MIN_AMOUNT',       25.00 );  // $25 minimum payout
define( 'PB_PAYOUT_COOLDOWN_DAYS',    30 );      // days between requests
define( 'PB_TG_SEND_SH',             '/home/jared/projects/AI-CIV/aether/tools/tg_send.sh' );

// ============================================================
// ACTIVATION — CREATE TABLES
// ============================================================
register_activation_hook( __FILE__, 'pb_referral_activate' );

function pb_referral_activate() {
    global $wpdb;
    $charset_collate = $wpdb->get_charset_collate();

    pb_referral_rewrite_rules();
    flush_rewrite_rules();

    // Main referrals table
    $table_referrals = $wpdb->prefix . PB_REFERRAL_TABLE;
    $sql_referrals = "CREATE TABLE IF NOT EXISTS {$table_referrals} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        referrer_code VARCHAR(8) NOT NULL,
        referrer_email VARCHAR(255) NOT NULL,
        referrer_name VARCHAR(255) NOT NULL,
        referred_email VARCHAR(255) DEFAULT NULL,
        referred_name VARCHAR(255) DEFAULT NULL,
        status VARCHAR(20) DEFAULT 'pending',
        earnings DECIMAL(10,2) DEFAULT 0.00,
        click_count INT DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        completed_at DATETIME DEFAULT NULL,
        KEY idx_referrer_code (referrer_code),
        KEY idx_referrer_email (referrer_email)
    ) {$charset_collate};";

    // Referral users table
    $table_users = $wpdb->prefix . PB_REFERRAL_USERS_TABLE;
    $sql_users = "CREATE TABLE IF NOT EXISTS {$table_users} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        email VARCHAR(255) UNIQUE NOT NULL,
        name VARCHAR(255) NOT NULL,
        referral_code VARCHAR(8) UNIQUE NOT NULL,
        total_referrals INT DEFAULT 0,
        pending_referrals INT DEFAULT 0,
        completed_referrals INT DEFAULT 0,
        total_earnings DECIMAL(10,2) DEFAULT 0.00,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    ) {$charset_collate};";

    // Reward ledger table (Phase 2)
    $table_ledger = $wpdb->prefix . PB_REFERRAL_LEDGER_TABLE;
    $sql_ledger = "CREATE TABLE IF NOT EXISTS {$table_ledger} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        referral_code VARCHAR(8) NOT NULL,
        event_type ENUM('conversion_credit','milestone_bonus','revenue_share') NOT NULL,
        amount DECIMAL(10,2) NOT NULL,
        source_referral_id BIGINT DEFAULT NULL,
        status ENUM('pending','approved','paid') DEFAULT 'pending',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_code (referral_code),
        KEY idx_status (status)
    ) {$charset_collate};";

    require_once ABSPATH . 'wp-admin/includes/upgrade.php';
    dbDelta( $sql_referrals );
    dbDelta( $sql_users );
    dbDelta( $sql_ledger );

    pb_referral_seed_data();
}

function pb_referral_seed_data() {
    global $wpdb;
    $table_users = $wpdb->prefix . PB_REFERRAL_USERS_TABLE;
    $table_refs  = $wpdb->prefix . PB_REFERRAL_TABLE;

    $exists = $wpdb->get_var( $wpdb->prepare(
        "SELECT id FROM {$table_users} WHERE email = %s",
        'jared@puretechnology.nyc'
    ) );

    if ( $exists ) return;

    $wpdb->insert( $table_users, [
        'email'                => 'jared@puretechnology.nyc',
        'name'                 => 'Jared Sanborn',
        'referral_code'        => 'JAREDSB0',
        'total_referrals'      => 2,
        'pending_referrals'    => 1,
        'completed_referrals'  => 1,
        'total_earnings'       => 5.00,
        'created_at'           => current_time( 'mysql' ),
    ] );

    $wpdb->insert( $table_refs, [
        'referrer_code'   => 'JAREDSB0',
        'referrer_email'  => 'jared@puretechnology.nyc',
        'referrer_name'   => 'Jared Sanborn',
        'referred_email'  => 'a***@example.com',
        'referred_name'   => 'Alex M.',
        'status'          => 'completed',
        'earnings'        => 5.00,
        'click_count'     => 3,
        'created_at'      => date( 'Y-m-d H:i:s', strtotime( '-14 days' ) ),
        'completed_at'    => date( 'Y-m-d H:i:s', strtotime( '-10 days' ) ),
    ] );

    $wpdb->insert( $table_refs, [
        'referrer_code'   => 'JAREDSB0',
        'referrer_email'  => 'jared@puretechnology.nyc',
        'referrer_name'   => 'Jared Sanborn',
        'referred_email'  => 's***@example.com',
        'referred_name'   => 'Sarah K.',
        'status'          => 'pending',
        'earnings'        => 0.00,
        'click_count'     => 1,
        'created_at'      => date( 'Y-m-d H:i:s', strtotime( '-3 days' ) ),
        'completed_at'    => null,
    ] );
}

// ============================================================
// URL REWRITE: /r/XXXXXXXX → /?ref=XXXXXXXX
// ============================================================
add_action( 'init', 'pb_referral_rewrite_rules' );
function pb_referral_rewrite_rules() {
    add_rewrite_rule( '^r/([A-Za-z0-9]{6,12})/?$', 'index.php?pb_referral_code=$matches[1]', 'top' );
}

add_filter( 'query_vars', function( $vars ) {
    $vars[] = 'pb_referral_code';
    return $vars;
} );

add_action( 'template_redirect', 'pb_referral_handle_redirect' );
function pb_referral_handle_redirect() {
    $code = get_query_var( 'pb_referral_code' );
    if ( ! $code ) return;

    pb_referral_increment_click( sanitize_text_field( $code ) );
    wp_redirect( home_url( '/?ref=' . urlencode( $code ) ), 302 );
    exit;
}

function pb_referral_increment_click( $code ) {
    global $wpdb;
    $table = $wpdb->prefix . PB_REFERRAL_TABLE;
    $wpdb->query( $wpdb->prepare(
        "UPDATE {$table} SET click_count = click_count + 1
         WHERE referrer_code = %s AND status = 'pending'
         ORDER BY created_at DESC LIMIT 1",
        $code
    ) );
}

// ============================================================
// PHASE 2: REFERRAL ATTRIBUTION — localStorage + Cookie injection
// Fires on wp_footer when ?ref= param is present in URL
// ============================================================
add_action( 'wp_footer', 'pb_referral_inject_attribution_script' );
function pb_referral_inject_attribution_script() {
    ?>
    <script>
    (function() {
        // Read ?ref= from URL query string
        var params = new URLSearchParams(window.location.search);
        var ref = params.get('ref');
        if (!ref) return;

        // Sanitize: only allow alphanumeric, 6-12 chars
        if (!/^[A-Za-z0-9]{6,12}$/.test(ref)) return;

        // Store in localStorage
        try {
            localStorage.setItem('pb_ref', ref);
        } catch(e) {}

        // Store in cookie (30-day expiry)
        var expires = new Date();
        expires.setDate(expires.getDate() + 30);
        document.cookie = 'pb_ref=' + encodeURIComponent(ref) +
            '; expires=' + expires.toUTCString() +
            '; path=/; SameSite=Lax';
    })();

    // Global helper: read stored referral code at payment time
    window.getPbRef = function() {
        try {
            var ls = localStorage.getItem('pb_ref');
            if (ls && /^[A-Za-z0-9]{6,12}$/.test(ls)) return ls;
        } catch(e) {}
        var match = document.cookie.match(/(?:^|;)\s*pb_ref=([^;]+)/);
        if (match) {
            var decoded = decodeURIComponent(match[1]);
            if (/^[A-Za-z0-9]{6,12}$/.test(decoded)) return decoded;
        }
        return null;
    };
    </script>
    <?php
}

// ============================================================
// PHASE 2: SHORTCODES
// ============================================================
add_action( 'init', 'pb_referral_register_shortcodes' );
function pb_referral_register_shortcodes() {
    add_shortcode( 'pb_referral_register', 'pb_referral_shortcode_register' );
    add_shortcode( 'pb_referral_dashboard', 'pb_referral_shortcode_dashboard' );
}

// ─────────────────────────────────────────────────────────────
// SHORTCODE: [pb_referral_register]
// Renders a name+email form. On success shows referral link.
// ─────────────────────────────────────────────────────────────
function pb_referral_shortcode_register( $atts ) {
    $nonce_action = 'pb_referral_register_action';

    ob_start();
    ?>
    <div id="pb-ref-register-wrap" style="
        background: #080a12;
        border: 1px solid #2a93c1;
        border-radius: 12px;
        padding: 32px;
        max-width: 480px;
        margin: 0 auto;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        color: #e0e6f0;
    ">
        <h2 style="color:#2a93c1; margin:0 0 8px; font-size:1.4rem;">Get Your Referral Link</h2>
        <p style="color:#8899aa; margin:0 0 24px; font-size:0.9rem;">
            Earn rewards when your friends join PureBrain.
        </p>

        <div id="pb-ref-register-form">
            <div style="margin-bottom:16px;">
                <label style="display:block; font-size:0.85rem; color:#8899aa; margin-bottom:6px;">Your Name</label>
                <input id="pb-ref-name" type="text" placeholder="Jane Smith" style="
                    width:100%; box-sizing:border-box;
                    background:#0e1220; border:1px solid #2a3550;
                    border-radius:8px; color:#e0e6f0;
                    padding:10px 14px; font-size:0.95rem; outline:none;
                " />
            </div>
            <div style="margin-bottom:20px;">
                <label style="display:block; font-size:0.85rem; color:#8899aa; margin-bottom:6px;">Email Address</label>
                <input id="pb-ref-email" type="email" placeholder="you@example.com" style="
                    width:100%; box-sizing:border-box;
                    background:#0e1220; border:1px solid #2a3550;
                    border-radius:8px; color:#e0e6f0;
                    padding:10px 14px; font-size:0.95rem; outline:none;
                " />
            </div>
            <div id="pb-ref-register-error" style="color:#f1420b; font-size:0.85rem; margin-bottom:12px; display:none;"></div>
            <button id="pb-ref-register-btn" onclick="pbRefRegister()" style="
                background:#f1420b; color:#fff;
                border:none; border-radius:8px;
                padding:12px 28px; font-size:1rem;
                cursor:pointer; width:100%;
                font-weight:600; letter-spacing:0.3px;
            ">Generate My Link</button>
        </div>

        <div id="pb-ref-register-success" style="display:none;">
            <p style="color:#2a93c1; font-size:0.95rem; margin:0 0 16px;">
                Your referral link is ready!
            </p>
            <div style="
                background:#0e1220; border:1px solid #2a3550;
                border-radius:8px; padding:12px 14px;
                display:flex; align-items:center; gap:10px;
            ">
                <span id="pb-ref-link-text" style="
                    flex:1; color:#e0e6f0; font-size:0.9rem;
                    word-break:break-all;
                "></span>
                <button onclick="pbRefCopyLink()" style="
                    background:#2a93c1; color:#fff;
                    border:none; border-radius:6px;
                    padding:6px 14px; cursor:pointer;
                    font-size:0.85rem; white-space:nowrap;
                " id="pb-ref-copy-btn">Copy</button>
            </div>
            <p style="color:#8899aa; font-size:0.8rem; margin:12px 0 0;">
                Share this link and earn $5 for every friend who joins.
            </p>
        </div>
    </div>

    <script>
    var _pbRefLink = '';

    function pbRefRegister() {
        var name  = document.getElementById('pb-ref-name').value.trim();
        var email = document.getElementById('pb-ref-email').value.trim();
        var errEl = document.getElementById('pb-ref-register-error');
        var btn   = document.getElementById('pb-ref-register-btn');

        errEl.style.display = 'none';

        if (!name) { errEl.textContent = 'Please enter your name.'; errEl.style.display='block'; return; }
        if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
            errEl.textContent = 'Please enter a valid email address.'; errEl.style.display='block'; return;
        }

        btn.textContent = 'Generating...';
        btn.disabled = true;

        fetch('<?php echo esc_url( rest_url( 'pb-referral/v1/register' ) ); ?>', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-WP-Nonce': '<?php echo esc_js( wp_create_nonce( 'wp_rest' ) ); ?>' },
            body: JSON.stringify({ name: name, email: email })
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.code) {
                errEl.textContent = data.message || 'Registration failed. Please try again.';
                errEl.style.display = 'block';
                btn.textContent = 'Generate My Link';
                btn.disabled = false;
                return;
            }
            _pbRefLink = data.referral_link;
            document.getElementById('pb-ref-link-text').textContent = data.referral_link;
            document.getElementById('pb-ref-register-form').style.display = 'none';
            document.getElementById('pb-ref-register-success').style.display = 'block';
        })
        .catch(function() {
            errEl.textContent = 'Network error. Please try again.';
            errEl.style.display = 'block';
            btn.textContent = 'Generate My Link';
            btn.disabled = false;
        });
    }

    function pbRefCopyLink() {
        if (!_pbRefLink) return;
        navigator.clipboard.writeText(_pbRefLink).then(function() {
            var btn = document.getElementById('pb-ref-copy-btn');
            btn.textContent = 'Copied!';
            btn.style.background = '#1a7a50';
            setTimeout(function() {
                btn.textContent = 'Copy';
                btn.style.background = '#2a93c1';
            }, 2000);
        });
    }
    </script>
    <?php
    return ob_get_clean();
}

// ─────────────────────────────────────────────────────────────
// SHORTCODE: [pb_referral_dashboard]
// Reads ?code= or ?email= from URL.
// Renders stats + history table + payout request section.
// ─────────────────────────────────────────────────────────────
function pb_referral_shortcode_dashboard( $atts ) {
    ob_start();
    ?>
    <div id="pb-ref-dashboard-wrap" style="
        background: #080a12;
        min-height: 300px;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        color: #e0e6f0;
    ">
        <div id="pb-ref-dashboard-loading" style="text-align:center; padding:48px; color:#8899aa;">
            Loading your dashboard...
        </div>
        <div id="pb-ref-dashboard-error" style="display:none; text-align:center; padding:48px; color:#f1420b;"></div>
        <div id="pb-ref-dashboard-content" style="display:none;">

            <!-- Stats cards row -->
            <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:16px; margin-bottom:28px;">
                <div class="pb-stat-card" style="background:#0e1220; border:1px solid #2a3550; border-radius:12px; padding:20px;">
                    <div style="font-size:0.8rem; color:#8899aa; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;">Total Clicks</div>
                    <div id="pb-stat-clicks" style="font-size:2rem; font-weight:700; color:#2a93c1;">—</div>
                </div>
                <div class="pb-stat-card" style="background:#0e1220; border:1px solid #2a3550; border-radius:12px; padding:20px;">
                    <div style="font-size:0.8rem; color:#8899aa; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;">Referrals</div>
                    <div id="pb-stat-referrals" style="font-size:2rem; font-weight:700; color:#2a93c1;">—</div>
                </div>
                <div class="pb-stat-card" style="background:#0e1220; border:1px solid #2a3550; border-radius:12px; padding:20px;">
                    <div style="font-size:0.8rem; color:#8899aa; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;">Completed</div>
                    <div id="pb-stat-completed" style="font-size:2rem; font-weight:700; color:#f1420b;">—</div>
                </div>
                <div class="pb-stat-card" style="background:#0e1220; border:1px solid #2a3550; border-radius:12px; padding:20px;">
                    <div style="font-size:0.8rem; color:#8899aa; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;">Earnings</div>
                    <div id="pb-stat-earnings" style="font-size:2rem; font-weight:700; color:#1a7a50;">—</div>
                </div>
            </div>

            <!-- Referral link bar -->
            <div style="background:#0e1220; border:1px solid #2a3550; border-radius:12px; padding:20px; margin-bottom:28px;">
                <div style="font-size:0.85rem; color:#8899aa; margin-bottom:10px;">Your Referral Link</div>
                <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                    <span id="pb-dash-link" style="flex:1; color:#2a93c1; font-size:0.95rem; word-break:break-all; min-width:200px;"></span>
                    <button onclick="pbDashCopyLink()" id="pb-dash-copy-btn" style="
                        background:#2a93c1; color:#fff; border:none;
                        border-radius:6px; padding:8px 18px;
                        cursor:pointer; font-size:0.85rem; white-space:nowrap;
                    ">Copy Link</button>
                </div>
            </div>

            <!-- Conversion rate banner -->
            <div id="pb-conv-rate-bar" style="
                background: linear-gradient(90deg, #0e1220 0%, #0a1a2f 100%);
                border:1px solid #2a3550; border-radius:12px;
                padding:16px 20px; margin-bottom:28px;
                display:flex; align-items:center; gap:16px; flex-wrap:wrap;
            ">
                <div>
                    <div style="font-size:0.8rem; color:#8899aa; margin-bottom:2px;">Conversion Rate</div>
                    <div id="pb-conv-rate" style="font-size:1.4rem; font-weight:700; color:#f1420b;">—%</div>
                </div>
                <div style="color:#2a3550; font-size:1.5rem;">|</div>
                <div>
                    <div style="font-size:0.8rem; color:#8899aa; margin-bottom:2px;">Pending</div>
                    <div id="pb-stat-pending" style="font-size:1.4rem; font-weight:700; color:#8899aa;">—</div>
                </div>
            </div>

            <!-- Reward tiers -->
            <div style="background:#0e1220; border:1px solid #2a3550; border-radius:12px; padding:20px; margin-bottom:28px;">
                <div style="font-size:0.9rem; color:#e0e6f0; font-weight:600; margin-bottom:14px;">How You Earn</div>
                <div id="pb-reward-tiers" style="display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px;"></div>
            </div>

            <!-- Referral history table -->
            <div style="background:#0e1220; border:1px solid #2a3550; border-radius:12px; padding:20px; margin-bottom:28px;">
                <div style="font-size:0.9rem; color:#e0e6f0; font-weight:600; margin-bottom:16px;">Referral History</div>
                <div id="pb-history-empty" style="display:none; color:#8899aa; font-size:0.9rem; text-align:center; padding:24px 0;">
                    No referrals yet. Share your link to get started!
                </div>
                <div id="pb-history-table-wrap" style="overflow-x:auto;">
                    <table id="pb-history-table" style="width:100%; border-collapse:collapse; font-size:0.875rem;">
                        <thead>
                            <tr style="border-bottom:1px solid #2a3550;">
                                <th style="text-align:left; padding:8px 12px; color:#8899aa; font-weight:500;">Person</th>
                                <th style="text-align:left; padding:8px 12px; color:#8899aa; font-weight:500;">Status</th>
                                <th style="text-align:left; padding:8px 12px; color:#8899aa; font-weight:500;">Earnings</th>
                                <th style="text-align:left; padding:8px 12px; color:#8899aa; font-weight:500;">Date</th>
                            </tr>
                        </thead>
                        <tbody id="pb-history-tbody"></tbody>
                    </table>
                </div>
            </div>

            <!-- ═══════════════════════════════════════════════════════ -->
            <!-- REQUEST PAYOUT SECTION (visible when earnings >= $25)  -->
            <!-- ═══════════════════════════════════════════════════════ -->
            <div id="pb-payout-section" style="display:none;">

                <!-- Payout request form -->
                <div style="background:#0e1220; border:1px solid #1a7a50; border-radius:12px; padding:24px; margin-bottom:20px;">
                    <div style="display:flex; align-items:center; gap:10px; margin-bottom:16px;">
                        <div style="
                            width:10px; height:10px; border-radius:50%;
                            background:#1a7a50; flex-shrink:0;
                        "></div>
                        <div style="font-size:1rem; color:#e0e6f0; font-weight:600;">Request Payout</div>
                    </div>
                    <p style="color:#8899aa; font-size:0.85rem; margin:0 0 20px; line-height:1.5;">
                        You have <strong id="pb-payout-available" style="color:#1a7a50;">$0.00</strong> available.
                        Minimum payout is $<?php echo number_format( PB_PAYOUT_MIN_AMOUNT, 0 ); ?>.
                        Payouts are processed within 2 business days via PayPal.
                    </p>

                    <div id="pb-payout-form">
                        <div style="margin-bottom:16px;">
                            <label style="display:block; font-size:0.85rem; color:#8899aa; margin-bottom:6px;">PayPal Email</label>
                            <input id="pb-payout-paypal-email" type="email" placeholder="you@paypal.com" style="
                                width:100%; box-sizing:border-box;
                                background:#080a12; border:1px solid #2a3550;
                                border-radius:8px; color:#e0e6f0;
                                padding:10px 14px; font-size:0.95rem; outline:none;
                            " />
                        </div>
                        <div style="margin-bottom:20px;">
                            <label style="display:block; font-size:0.85rem; color:#8899aa; margin-bottom:6px;">Amount to Request ($)</label>
                            <input id="pb-payout-amount" type="number" min="<?php echo PB_PAYOUT_MIN_AMOUNT; ?>" step="0.01" placeholder="25.00" style="
                                width:100%; box-sizing:border-box;
                                background:#080a12; border:1px solid #2a3550;
                                border-radius:8px; color:#e0e6f0;
                                padding:10px 14px; font-size:0.95rem; outline:none;
                            " />
                            <div style="font-size:0.78rem; color:#8899aa; margin-top:5px;">
                                Must be between $<?php echo number_format( PB_PAYOUT_MIN_AMOUNT, 0 ); ?> and your available balance.
                            </div>
                        </div>
                        <div id="pb-payout-error" style="color:#f1420b; font-size:0.85rem; margin-bottom:14px; display:none; padding:10px 14px; background:rgba(241,66,11,0.08); border-radius:8px; border:1px solid rgba(241,66,11,0.2);"></div>
                        <button id="pb-payout-submit-btn" onclick="pbPayoutSubmit()" style="
                            background:#1a7a50; color:#fff;
                            border:none; border-radius:8px;
                            padding:12px 28px; font-size:1rem;
                            cursor:pointer; font-weight:600;
                            letter-spacing:0.3px;
                        ">Request Payout</button>
                    </div>

                    <div id="pb-payout-success" style="display:none; text-align:center; padding:16px 0;">
                        <div style="font-size:1.5rem; margin-bottom:8px;">&#10003;</div>
                        <div style="color:#1a7a50; font-weight:600; font-size:1rem; margin-bottom:6px;">Payout Request Submitted</div>
                        <div style="color:#8899aa; font-size:0.85rem;">Jared will process your payout within 2 business days.</div>
                    </div>
                </div>

                <!-- Payout history table -->
                <div id="pb-payout-history-wrap" style="background:#0e1220; border:1px solid #2a3550; border-radius:12px; padding:24px;">
                    <div style="font-size:0.9rem; color:#e0e6f0; font-weight:600; margin-bottom:16px;">Payout History</div>
                    <div id="pb-payout-history-loading" style="color:#8899aa; font-size:0.85rem; text-align:center; padding:16px 0;">Loading...</div>
                    <div id="pb-payout-history-empty" style="display:none; color:#8899aa; font-size:0.9rem; text-align:center; padding:16px 0;">
                        No payout requests yet.
                    </div>
                    <div id="pb-payout-history-table-wrap" style="display:none; overflow-x:auto;">
                        <table style="width:100%; border-collapse:collapse; font-size:0.875rem;">
                            <thead>
                                <tr style="border-bottom:1px solid #2a3550;">
                                    <th style="text-align:left; padding:8px 12px; color:#8899aa; font-weight:500;">Date</th>
                                    <th style="text-align:left; padding:8px 12px; color:#8899aa; font-weight:500;">Amount</th>
                                    <th style="text-align:left; padding:8px 12px; color:#8899aa; font-weight:500;">PayPal</th>
                                    <th style="text-align:left; padding:8px 12px; color:#8899aa; font-weight:500;">Status</th>
                                </tr>
                            </thead>
                            <tbody id="pb-payout-history-tbody"></tbody>
                        </table>
                    </div>
                </div>

            </div>
            <!-- /REQUEST PAYOUT SECTION -->

            <!-- Earnings too low notice (visible when earnings < $25) -->
            <div id="pb-payout-locked" style="display:none;
                background:#0e1220; border:1px solid #2a3550; border-radius:12px;
                padding:20px; text-align:center;
            ">
                <div style="color:#8899aa; font-size:0.85rem; line-height:1.6;">
                    You need at least <strong style="color:#e0e6f0;">$<?php echo number_format( PB_PAYOUT_MIN_AMOUNT, 0 ); ?></strong> in earnings to request a payout.
                    Keep referring to unlock payouts!
                </div>
            </div>

        </div>
    </div>

    <script>
    (function() {
        var apiBase   = '<?php echo esc_js( rest_url( 'pb-referral/v1' ) ); ?>';
        var wpNonce   = '<?php echo esc_js( wp_create_nonce( 'wp_rest' ) ); ?>';
        var params    = new URLSearchParams(window.location.search);
        var code      = params.get('code');
        var email     = params.get('email');
        var _dashCode = '';   // resolved referral code after dashboard load
        var _dashEarnings = 0;

        if (!code && !email) {
            document.getElementById('pb-ref-dashboard-loading').style.display = 'none';
            document.getElementById('pb-ref-dashboard-error').textContent =
                'No referral code found. Add ?code=YOUR_CODE to the URL.';
            document.getElementById('pb-ref-dashboard-error').style.display = 'block';
            return;
        }

        var url = apiBase + '/dashboard?' + (code ? 'code=' + encodeURIComponent(code) : 'email=' + encodeURIComponent(email));

        fetch(url)
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.code && d.code !== 'success') {
                document.getElementById('pb-ref-dashboard-loading').style.display = 'none';
                document.getElementById('pb-ref-dashboard-error').textContent = d.message || 'Could not load dashboard.';
                document.getElementById('pb-ref-dashboard-error').style.display = 'block';
                return;
            }

            // Calculate total clicks from history
            var totalClicks = 0;
            if (d.history) {
                d.history.forEach(function(r) { totalClicks += parseInt(r.click_count || 0, 10); });
            }

            var convRate = d.total_referrals > 0
                ? Math.round((d.completed / d.total_referrals) * 100) : 0;

            _dashCode     = d.referral_code;
            _dashEarnings = parseFloat(d.earnings) || 0;

            document.getElementById('pb-stat-clicks').textContent     = totalClicks;
            document.getElementById('pb-stat-referrals').textContent  = d.total_referrals;
            document.getElementById('pb-stat-completed').textContent  = d.completed;
            document.getElementById('pb-stat-earnings').textContent   = '$' + _dashEarnings.toFixed(2);
            document.getElementById('pb-stat-pending').textContent    = d.pending;
            document.getElementById('pb-conv-rate').textContent       = convRate + '%';
            document.getElementById('pb-dash-link').textContent       = d.referral_link;
            window._pbDashLink = d.referral_link;

            // Reward tiers
            if (d.reward_tiers) {
                var tiersHtml = '';
                d.reward_tiers.forEach(function(t) {
                    tiersHtml += '<div style="background:#080a12; border:1px solid #1a2540; border-radius:8px; padding:12px 14px;">' +
                        '<div style="font-size:0.78rem; color:#8899aa; margin-bottom:4px;">' + pbEscape(t.label) + '</div>' +
                        '<div style="font-size:1rem; font-weight:600; color:#f1420b;">' + pbEscape(t.reward) + '</div>' +
                        '</div>';
                });
                document.getElementById('pb-reward-tiers').innerHTML = tiersHtml;
            }

            // History rows
            var tbody = document.getElementById('pb-history-tbody');
            if (!d.history || d.history.length === 0) {
                document.getElementById('pb-history-table-wrap').style.display = 'none';
                document.getElementById('pb-history-empty').style.display = 'block';
            } else {
                var rows = '';
                d.history.forEach(function(r) {
                    var statusColor = r.status === 'completed' ? '#1a7a50' : '#8899aa';
                    var statusLabel = r.status === 'completed' ? 'Converted' : 'Pending';
                    var dateStr = r.created_at ? r.created_at.slice(0, 10) : '—';
                    var earningsStr = parseFloat(r.earnings || 0) > 0 ? '$' + parseFloat(r.earnings).toFixed(2) : '—';
                    rows += '<tr style="border-bottom:1px solid #1a2540;">' +
                        '<td style="padding:10px 12px; color:#e0e6f0;">' + pbEscape(r.referred_name || r.referred_email || '—') + '</td>' +
                        '<td style="padding:10px 12px;"><span style="color:' + statusColor + '; font-weight:500;">' + statusLabel + '</span></td>' +
                        '<td style="padding:10px 12px; color:#1a7a50;">' + earningsStr + '</td>' +
                        '<td style="padding:10px 12px; color:#8899aa;">' + pbEscape(dateStr) + '</td>' +
                        '</tr>';
                });
                tbody.innerHTML = rows;
            }

            document.getElementById('pb-ref-dashboard-loading').style.display = 'none';
            document.getElementById('pb-ref-dashboard-content').style.display = 'block';

            // Show payout section based on earnings threshold
            var minPayout = <?php echo PB_PAYOUT_MIN_AMOUNT; ?>;
            if (_dashEarnings >= minPayout) {
                document.getElementById('pb-payout-section').style.display = 'block';
                document.getElementById('pb-payout-available').textContent = '$' + _dashEarnings.toFixed(2);
                document.getElementById('pb-payout-amount').max = _dashEarnings.toFixed(2);
                pbLoadPayoutHistory(_dashCode);
            } else {
                document.getElementById('pb-payout-locked').style.display = 'block';
            }
        })
        .catch(function() {
            document.getElementById('pb-ref-dashboard-loading').style.display = 'none';
            document.getElementById('pb-ref-dashboard-error').textContent = 'Failed to load dashboard. Please try again.';
            document.getElementById('pb-ref-dashboard-error').style.display = 'block';
        });

        // ─────────────────────────────────────────────────────
        // Load payout history
        // ─────────────────────────────────────────────────────
        function pbLoadPayoutHistory(referralCode) {
            fetch(apiBase + '/payout-history?referral_code=' + encodeURIComponent(referralCode), {
                headers: { 'X-WP-Nonce': wpNonce }
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                document.getElementById('pb-payout-history-loading').style.display = 'none';
                if (!data.requests || data.requests.length === 0) {
                    document.getElementById('pb-payout-history-empty').style.display = 'block';
                    return;
                }
                var rows = '';
                data.requests.forEach(function(req) {
                    var dateStr = req.created_at ? req.created_at.slice(0, 10) : '—';
                    var statusMap = { pending: '#8899aa', approved: '#2a93c1', paid: '#1a7a50' };
                    var statusColor = statusMap[req.status] || '#8899aa';
                    var statusLabel = req.status ? (req.status.charAt(0).toUpperCase() + req.status.slice(1)) : '—';
                    rows += '<tr style="border-bottom:1px solid #1a2540;">' +
                        '<td style="padding:10px 12px; color:#8899aa;">' + pbEscape(dateStr) + '</td>' +
                        '<td style="padding:10px 12px; color:#1a7a50; font-weight:600;">$' + parseFloat(req.amount || 0).toFixed(2) + '</td>' +
                        '<td style="padding:10px 12px; color:#e0e6f0;">' + pbEscape(req.paypal_email || '—') + '</td>' +
                        '<td style="padding:10px 12px;"><span style="color:' + statusColor + '; font-weight:500;">' + statusLabel + '</span></td>' +
                        '</tr>';
                });
                document.getElementById('pb-payout-history-tbody').innerHTML = rows;
                document.getElementById('pb-payout-history-table-wrap').style.display = 'block';
            })
            .catch(function() {
                document.getElementById('pb-payout-history-loading').style.display = 'none';
                document.getElementById('pb-payout-history-empty').textContent = 'Could not load payout history.';
                document.getElementById('pb-payout-history-empty').style.display = 'block';
            });
        }

        // ─────────────────────────────────────────────────────
        // Submit payout request
        // ─────────────────────────────────────────────────────
        window.pbPayoutSubmit = function() {
            var paypalEmail = document.getElementById('pb-payout-paypal-email').value.trim();
            var amount      = parseFloat(document.getElementById('pb-payout-amount').value);
            var errEl       = document.getElementById('pb-payout-error');
            var btn         = document.getElementById('pb-payout-submit-btn');

            errEl.style.display = 'none';

            if (!paypalEmail || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(paypalEmail)) {
                errEl.textContent = 'Please enter a valid PayPal email address.';
                errEl.style.display = 'block';
                return;
            }
            var minPayout = <?php echo PB_PAYOUT_MIN_AMOUNT; ?>;
            if (isNaN(amount) || amount < minPayout) {
                errEl.textContent = 'Minimum payout amount is $' + minPayout.toFixed(0) + '.';
                errEl.style.display = 'block';
                return;
            }
            if (amount > _dashEarnings) {
                errEl.textContent = 'Amount cannot exceed your available earnings of $' + _dashEarnings.toFixed(2) + '.';
                errEl.style.display = 'block';
                return;
            }

            btn.textContent = 'Submitting...';
            btn.disabled = true;

            fetch(apiBase + '/payout-request', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-WP-Nonce': wpNonce
                },
                body: JSON.stringify({
                    referral_code: _dashCode,
                    paypal_email: paypalEmail,
                    amount: amount
                })
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.code || data.error) {
                    errEl.textContent = data.message || data.error || 'Request failed. Please try again.';
                    errEl.style.display = 'block';
                    btn.textContent = 'Request Payout';
                    btn.disabled = false;
                    return;
                }
                document.getElementById('pb-payout-form').style.display = 'none';
                document.getElementById('pb-payout-success').style.display = 'block';
            })
            .catch(function() {
                errEl.textContent = 'Network error. Please try again.';
                errEl.style.display = 'block';
                btn.textContent = 'Request Payout';
                btn.disabled = false;
            });
        };

        function pbEscape(str) {
            if (!str) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }
    })();

    window._pbDashLink = '';
    function pbDashCopyLink() {
        if (!window._pbDashLink) return;
        navigator.clipboard.writeText(window._pbDashLink).then(function() {
            var btn = document.getElementById('pb-dash-copy-btn');
            btn.textContent = 'Copied!';
            btn.style.background = '#1a7a50';
            setTimeout(function() {
                btn.textContent = 'Copy Link';
                btn.style.background = '#2a93c1';
            }, 2000);
        });
    }
    </script>
    <?php
    return ob_get_clean();
}

// ============================================================
// FLUSH REWRITE RULES
// ============================================================
register_deactivation_hook( __FILE__, function() {
    flush_rewrite_rules();
} );

// ============================================================
// REST API ENDPOINTS
// ============================================================
add_action( 'rest_api_init', 'pb_referral_register_routes' );

function pb_referral_register_routes() {
    $ns = 'pb-referral/v1';

    // GET /dashboard?code=XXXXXXXX
    register_rest_route( $ns, '/dashboard', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => 'pb_referral_api_dashboard',
        'permission_callback' => '__return_true',
        'args'                => [
            'code'  => [ 'sanitize_callback' => 'sanitize_text_field' ],
            'email' => [ 'sanitize_callback' => 'sanitize_email' ],
        ],
    ] );

    // POST /register  (rate-limited: 3 per IP per hour)
    register_rest_route( $ns, '/register', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => 'pb_referral_api_register',
        'permission_callback' => '__return_true',
    ] );

    // POST /click
    register_rest_route( $ns, '/click', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => 'pb_referral_api_click',
        'permission_callback' => '__return_true',
    ] );

    // POST /convert  (idempotent by referred_email + referrer_code)
    register_rest_route( $ns, '/convert', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => 'pb_referral_api_convert',
        'permission_callback' => '__return_true',
    ] );

    // GET /lookup?email=xxx
    register_rest_route( $ns, '/lookup', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => 'pb_referral_api_lookup',
        'permission_callback' => '__return_true',
        'args'                => [
            'email' => [ 'sanitize_callback' => 'sanitize_email', 'required' => true ],
        ],
    ] );

    // GET /rewards?code=XXXXXXXX  (Phase 2: reward ledger)
    register_rest_route( $ns, '/rewards', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => 'pb_referral_api_rewards',
        'permission_callback' => '__return_true',
        'args'                => [
            'code' => [ 'sanitize_callback' => 'sanitize_text_field', 'required' => true ],
        ],
    ] );

    // POST /payout-request  (Phase 3: payout requests via website)
    register_rest_route( $ns, '/payout-request', [
        'methods'             => WP_REST_Server::CREATABLE,
        'callback'            => 'pb_referral_api_payout_request',
        'permission_callback' => '__return_true',
    ] );

    // GET /payout-history?referral_code=XXXXXXXX  (Phase 3)
    register_rest_route( $ns, '/payout-history', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => 'pb_referral_api_payout_history_wp',
        'permission_callback' => '__return_true',
        'args'                => [
            'referral_code' => [ 'sanitize_callback' => 'sanitize_text_field', 'required' => true ],
        ],
    ] );
}

// ─────────────────────────────────────────────────────────────
// GET /dashboard?code=XXXXXXXX
// ─────────────────────────────────────────────────────────────
function pb_referral_api_dashboard( WP_REST_Request $request ) {
    global $wpdb;
    $table_users = $wpdb->prefix . PB_REFERRAL_USERS_TABLE;
    $table_refs  = $wpdb->prefix . PB_REFERRAL_TABLE;

    $code  = $request->get_param( 'code' );
    $email = $request->get_param( 'email' );

    if ( ! $code && ! $email ) {
        return new WP_Error( 'missing_param', 'Provide code or email.', [ 'status' => 400 ] );
    }

    if ( $code ) {
        $user = $wpdb->get_row( $wpdb->prepare(
            "SELECT * FROM {$table_users} WHERE referral_code = %s",
            $code
        ) );
    } else {
        $user = $wpdb->get_row( $wpdb->prepare(
            "SELECT * FROM {$table_users} WHERE email = %s",
            $email
        ) );
    }

    if ( ! $user ) {
        return new WP_Error( 'not_found', 'Referral code not found.', [ 'status' => 404 ] );
    }

    $history = $wpdb->get_results( $wpdb->prepare(
        "SELECT referred_name, referred_email, status, earnings, click_count, created_at, completed_at
         FROM {$table_refs}
         WHERE referrer_code = %s
         ORDER BY created_at DESC
         LIMIT 50",
        $user->referral_code
    ) );

    $referral_link = home_url( '/r/' . $user->referral_code );

    $reward_tiers = [
        [ 'label' => 'Per successful referral', 'reward' => '$' . number_format( PB_REFERRAL_BASE_CREDIT, 2 ) . ' credit' ],
        [ 'label' => '5+ referrals bonus',      'reward' => '+$' . number_format( PB_REFERRAL_BONUS_AMOUNT, 2 ) . ' bonus' ],
        [ 'label' => 'Revenue share (ongoing)',  'reward' => number_format( PB_REFERRAL_REVENUE_SHARE * 100, 0 ) . '% lifetime' ],
        [ 'label' => 'Reward activates after',   'reward' => PB_REFERRAL_REWARD_DAYS . ' days active' ],
    ];

    return rest_ensure_response( [
        'referral_code'   => $user->referral_code,
        'referral_link'   => $referral_link,
        'name'            => $user->name,
        'email'           => $user->email,
        'total_referrals' => (int) $user->total_referrals,
        'pending'         => (int) $user->pending_referrals,
        'completed'       => (int) $user->completed_referrals,
        'earnings'        => (float) $user->total_earnings,
        'history'         => $history,
        'reward_tiers'    => $reward_tiers,
    ] );
}

// ─────────────────────────────────────────────────────────────
// POST /register  (Phase 2: rate-limited at 3/IP/hour)
// ─────────────────────────────────────────────────────────────
function pb_referral_api_register( WP_REST_Request $request ) {
    global $wpdb;
    $table_users = $wpdb->prefix . PB_REFERRAL_USERS_TABLE;

    // Rate limit: max 3 registrations per IP per hour
    $ip          = sanitize_text_field( $_SERVER['REMOTE_ADDR'] ?? '' );
    $rate_key    = 'pb_ref_reg_' . md5( $ip );
    $reg_count   = (int) get_transient( $rate_key );
    if ( $reg_count >= 3 ) {
        return new WP_Error( 'rate_limited', 'Too many registration attempts. Please try again later.', [ 'status' => 429 ] );
    }

    $email = sanitize_email( $request->get_param( 'email' ) );
    $name  = sanitize_text_field( $request->get_param( 'name' ) );

    if ( ! is_email( $email ) || empty( $name ) ) {
        return new WP_Error( 'invalid_input', 'Valid email and name required.', [ 'status' => 400 ] );
    }

    $existing = $wpdb->get_row( $wpdb->prepare(
        "SELECT * FROM {$table_users} WHERE email = %s",
        $email
    ) );

    if ( $existing ) {
        return rest_ensure_response( [
            'referral_code'  => $existing->referral_code,
            'referral_link'  => home_url( '/r/' . $existing->referral_code ),
            'already_exists' => true,
        ] );
    }

    $code = pb_referral_generate_code();

    $inserted = $wpdb->insert( $table_users, [
        'email'                => $email,
        'name'                 => $name,
        'referral_code'        => $code,
        'total_referrals'      => 0,
        'pending_referrals'    => 0,
        'completed_referrals'  => 0,
        'total_earnings'       => 0.00,
        'created_at'           => current_time( 'mysql' ),
    ] );

    if ( ! $inserted ) {
        return new WP_Error( 'db_error', 'Could not register user.', [ 'status' => 500 ] );
    }

    // Increment rate limit counter
    set_transient( $rate_key, $reg_count + 1, HOUR_IN_SECONDS );

    return rest_ensure_response( [
        'referral_code'  => $code,
        'referral_link'  => home_url( '/r/' . $code ),
        'already_exists' => false,
    ] );
}

// ─────────────────────────────────────────────────────────────
// POST /click
// ─────────────────────────────────────────────────────────────
function pb_referral_api_click( WP_REST_Request $request ) {
    $code = sanitize_text_field( $request->get_param( 'code' ) );
    if ( empty( $code ) ) {
        return new WP_Error( 'missing_code', 'code required.', [ 'status' => 400 ] );
    }

    $transient_key = 'pb_ref_click_' . md5( $code . '_' . ( $_SERVER['REMOTE_ADDR'] ?? '' ) );
    $count = (int) get_transient( $transient_key );
    if ( $count >= 20 ) {
        return rest_ensure_response( [ 'ok' => true, 'rate_limited' => true ] );
    }
    set_transient( $transient_key, $count + 1, HOUR_IN_SECONDS );

    pb_referral_increment_click( $code );
    return rest_ensure_response( [ 'ok' => true ] );
}

// ─────────────────────────────────────────────────────────────
// POST /convert  (Phase 2: idempotent + ledger write + email)
// ─────────────────────────────────────────────────────────────
function pb_referral_api_convert( WP_REST_Request $request ) {
    global $wpdb;
    $table_users  = $wpdb->prefix . PB_REFERRAL_USERS_TABLE;
    $table_refs   = $wpdb->prefix . PB_REFERRAL_TABLE;
    $table_ledger = $wpdb->prefix . PB_REFERRAL_LEDGER_TABLE;

    $referrer_code  = sanitize_text_field( $request->get_param( 'referrer_code' ) );
    $referred_email = sanitize_email( $request->get_param( 'referred_email' ) );
    $referred_name  = sanitize_text_field( $request->get_param( 'referred_name' ) );
    $amount         = floatval( $request->get_param( 'amount' ) );

    if ( empty( $referrer_code ) || ! is_email( $referred_email ) ) {
        return new WP_Error( 'invalid_input', 'referrer_code and valid referred_email required.', [ 'status' => 400 ] );
    }

    // IDEMPOTENCY: Prevent duplicate conversions for the same referred email + referrer code
    $already = $wpdb->get_var( $wpdb->prepare(
        "SELECT id FROM {$table_refs}
         WHERE referrer_code = %s
         AND (referred_email = %s OR referred_email = %s)
         AND status = 'completed'
         LIMIT 1",
        $referrer_code,
        $referred_email,
        pb_referral_mask_email( $referred_email )
    ) );

    if ( $already ) {
        return rest_ensure_response( [
            'ok'        => true,
            'duplicate' => true,
            'message'   => 'Referral already recorded for this email.',
        ] );
    }

    $referrer = $wpdb->get_row( $wpdb->prepare(
        "SELECT * FROM {$table_users} WHERE referral_code = %s",
        $referrer_code
    ) );

    if ( ! $referrer ) {
        return new WP_Error( 'not_found', 'Referrer not found.', [ 'status' => 404 ] );
    }

    // Calculate earnings
    $revenue_share = $amount > 0 ? round( $amount * PB_REFERRAL_REVENUE_SHARE, 2 ) : PB_REFERRAL_BASE_CREDIT;
    $earnings      = max( PB_REFERRAL_BASE_CREDIT, $revenue_share );
    $new_completed = (int) $referrer->completed_referrals + 1;
    $milestone_bonus = false;

    if ( $new_completed === PB_REFERRAL_BONUS_THRESHOLD ) {
        $earnings       += PB_REFERRAL_BONUS_AMOUNT;
        $milestone_bonus = true;
    }

    $masked_email = pb_referral_mask_email( $referred_email );

    // Insert referral record
    $insert_id = null;
    $wpdb->insert( $table_refs, [
        'referrer_code'   => $referrer_code,
        'referrer_email'  => $referrer->email,
        'referrer_name'   => $referrer->name,
        'referred_email'  => $masked_email,
        'referred_name'   => $referred_name ?: 'New Member',
        'status'          => 'completed',
        'earnings'        => $earnings,
        'click_count'     => 0,
        'created_at'      => current_time( 'mysql' ),
        'completed_at'    => current_time( 'mysql' ),
    ] );
    $insert_id = $wpdb->insert_id;

    // Update referrer stats
    $wpdb->update(
        $table_users,
        [
            'total_referrals'     => $referrer->total_referrals + 1,
            'completed_referrals' => $new_completed,
            'total_earnings'      => $referrer->total_earnings + $earnings,
        ],
        [ 'referral_code' => $referrer_code ]
    );

    // Write to reward ledger — base credit
    $base_credit = $amount > 0 ? round( $amount * PB_REFERRAL_REVENUE_SHARE, 2 ) : PB_REFERRAL_BASE_CREDIT;
    $base_credit = max( PB_REFERRAL_BASE_CREDIT, $base_credit );
    $wpdb->insert( $table_ledger, [
        'referral_code'      => $referrer_code,
        'event_type'         => $amount > 0 ? 'revenue_share' : 'conversion_credit',
        'amount'             => $base_credit,
        'source_referral_id' => $insert_id,
        'status'             => 'pending',
        'created_at'         => current_time( 'mysql' ),
    ] );

    // Write milestone bonus ledger row if applicable
    if ( $milestone_bonus ) {
        $wpdb->insert( $table_ledger, [
            'referral_code'      => $referrer_code,
            'event_type'         => 'milestone_bonus',
            'amount'             => PB_REFERRAL_BONUS_AMOUNT,
            'source_referral_id' => $insert_id,
            'status'             => 'pending',
            'created_at'         => current_time( 'mysql' ),
        ] );
    }

    // Email notification to referrer
    pb_referral_notify_referrer_conversion( $referrer, $masked_email, $referred_name ?: 'New Member', $earnings, $milestone_bonus );

    return rest_ensure_response( [
        'ok'           => true,
        'duplicate'    => false,
        'earnings'     => $earnings,
        'bonus_earned' => $milestone_bonus,
    ] );
}

// ─────────────────────────────────────────────────────────────
// GET /lookup?email=xxx
// ─────────────────────────────────────────────────────────────
function pb_referral_api_lookup( WP_REST_Request $request ) {
    global $wpdb;
    $table_users = $wpdb->prefix . PB_REFERRAL_USERS_TABLE;

    $email = sanitize_email( $request->get_param( 'email' ) );

    if ( ! is_email( $email ) ) {
        return new WP_Error( 'invalid_email', 'Valid email required.', [ 'status' => 400 ] );
    }

    $user = $wpdb->get_row( $wpdb->prepare(
        "SELECT referral_code, name, total_earnings, total_referrals FROM {$table_users} WHERE email = %s",
        $email
    ) );

    if ( ! $user ) {
        return new WP_Error( 'not_found', 'No referral account for that email.', [ 'status' => 404 ] );
    }

    return rest_ensure_response( [
        'referral_code'   => $user->referral_code,
        'referral_link'   => home_url( '/r/' . $user->referral_code ),
        'name'            => $user->name,
        'total_earnings'  => (float) $user->total_earnings,
        'total_referrals' => (int) $user->total_referrals,
    ] );
}

// ─────────────────────────────────────────────────────────────
// GET /rewards?code=XXXXXXXX  (Phase 2)
// ─────────────────────────────────────────────────────────────
function pb_referral_api_rewards( WP_REST_Request $request ) {
    global $wpdb;
    $table_ledger = $wpdb->prefix . PB_REFERRAL_LEDGER_TABLE;
    $table_users  = $wpdb->prefix . PB_REFERRAL_USERS_TABLE;

    $code = sanitize_text_field( $request->get_param( 'code' ) );

    // Verify code exists
    $user = $wpdb->get_var( $wpdb->prepare(
        "SELECT id FROM {$table_users} WHERE referral_code = %s",
        $code
    ) );

    if ( ! $user ) {
        return new WP_Error( 'not_found', 'Referral code not found.', [ 'status' => 404 ] );
    }

    $ledger = $wpdb->get_results( $wpdb->prepare(
        "SELECT id, event_type, amount, status, created_at
         FROM {$table_ledger}
         WHERE referral_code = %s
         ORDER BY created_at DESC
         LIMIT 100",
        $code
    ) );

    $totals = $wpdb->get_row( $wpdb->prepare(
        "SELECT
            SUM(amount) as total,
            SUM(CASE WHEN status='pending' THEN amount ELSE 0 END) as pending,
            SUM(CASE WHEN status='approved' THEN amount ELSE 0 END) as approved,
            SUM(CASE WHEN status='paid' THEN amount ELSE 0 END) as paid
         FROM {$table_ledger}
         WHERE referral_code = %s",
        $code
    ) );

    return rest_ensure_response( [
        'referral_code' => $code,
        'ledger'        => $ledger,
        'totals'        => [
            'total'    => (float) ( $totals->total   ?? 0 ),
            'pending'  => (float) ( $totals->pending  ?? 0 ),
            'approved' => (float) ( $totals->approved ?? 0 ),
            'paid'     => (float) ( $totals->paid     ?? 0 ),
        ],
    ] );
}

// ─────────────────────────────────────────────────────────────
// POST /payout-request  (Phase 3: payout via website)
// Body: { referral_code, paypal_email, amount }
// Validates earnings >= amount >= $25, writes to shared JSONL,
// notifies Jared via tg_send.sh.
// ─────────────────────────────────────────────────────────────
function pb_referral_api_payout_request( WP_REST_Request $request ) {
    global $wpdb;
    $table_users = $wpdb->prefix . PB_REFERRAL_USERS_TABLE;

    // Rate limit: 2 payout requests per IP per hour
    $ip        = sanitize_text_field( $_SERVER['REMOTE_ADDR'] ?? '' );
    $rate_key  = 'pb_payout_req_' . md5( $ip );
    $req_count = (int) get_transient( $rate_key );
    if ( $req_count >= 2 ) {
        return new WP_Error( 'rate_limited', 'Too many payout requests. Please try again later.', [ 'status' => 429 ] );
    }

    $referral_code = sanitize_text_field( $request->get_param( 'referral_code' ) );
    $paypal_email  = sanitize_email( $request->get_param( 'paypal_email' ) );
    $amount        = floatval( $request->get_param( 'amount' ) );

    // Validate inputs
    if ( empty( $referral_code ) || ! preg_match( '/^[A-Za-z0-9]{6,12}$/', $referral_code ) ) {
        return new WP_Error( 'invalid_input', 'Valid referral_code required.', [ 'status' => 400 ] );
    }
    if ( ! is_email( $paypal_email ) ) {
        return new WP_Error( 'invalid_input', 'Valid PayPal email required.', [ 'status' => 400 ] );
    }
    if ( $amount < PB_PAYOUT_MIN_AMOUNT ) {
        return new WP_Error(
            'amount_too_low',
            'Minimum payout amount is $' . number_format( PB_PAYOUT_MIN_AMOUNT, 0 ) . '.',
            [ 'status' => 400 ]
        );
    }

    // Look up referrer and verify earnings
    $user = $wpdb->get_row( $wpdb->prepare(
        "SELECT * FROM {$table_users} WHERE referral_code = %s",
        $referral_code
    ) );

    if ( ! $user ) {
        return new WP_Error( 'not_found', 'Referral code not found.', [ 'status' => 404 ] );
    }

    $actual_earnings = (float) $user->total_earnings;
    if ( $amount > $actual_earnings ) {
        return new WP_Error(
            'insufficient_earnings',
            sprintf(
                'Requested amount ($%.2f) exceeds available earnings ($%.2f).',
                $amount,
                $actual_earnings
            ),
            [ 'status' => 400 ]
        );
    }

    // Cooldown check: no pending payout in last 30 days for this code
    $all_requests = pb_referral_read_payout_requests();
    $cooldown_secs = PB_PAYOUT_COOLDOWN_DAYS * DAY_IN_SECONDS;
    $now_ts        = time();
    foreach ( $all_requests as $req ) {
        if (
            isset( $req['referral_code'] ) &&
            $req['referral_code'] === $referral_code &&
            isset( $req['status'] ) &&
            in_array( $req['status'], [ 'pending', 'approved' ], true ) &&
            isset( $req['created_at_ts'] ) &&
            ( $now_ts - (float) $req['created_at_ts'] ) < $cooldown_secs
        ) {
            $days_left = (int) ceil( ( $cooldown_secs - ( $now_ts - (float) $req['created_at_ts'] ) ) / DAY_IN_SECONDS );
            return new WP_Error(
                'cooldown',
                "A payout is already pending. Please wait {$days_left} more day(s).",
                [ 'status' => 429 ]
            );
        }
    }

    // Build request record — same schema as portal server
    $request_id = 'payout-' . $referral_code . '-' . $now_ts;
    $entry = [
        'request_id'     => $request_id,
        'referral_code'  => $referral_code,
        'paypal_email'   => $paypal_email,
        'amount'         => round( $amount, 2 ),
        'status'         => 'pending',
        'created_at'     => gmdate( 'c' ),
        'created_at_ts'  => $now_ts,
        'paid_at'        => null,
        'notes'          => '',
        'source'         => 'website',
    ];

    // Write to shared JSONL
    if ( ! pb_referral_write_payout_request( $entry ) ) {
        return new WP_Error( 'write_error', 'Could not record payout request. Please try again.', [ 'status' => 500 ] );
    }

    // Increment rate limit counter
    set_transient( $rate_key, $req_count + 1, HOUR_IN_SECONDS );

    // Notify Jared via tg_send.sh
    $tg_msg = sprintf(
        "PAYOUT REQUEST (website)\nReferral: %s\nAmount: $%.2f\nPayPal: %s\nRequest ID: %s\nEarnings on file: $%.2f",
        $referral_code,
        $amount,
        $paypal_email,
        $request_id,
        $actual_earnings
    );
    pb_referral_tg_notify( $tg_msg );

    return rest_ensure_response( [
        'ok'          => true,
        'request_id'  => $request_id,
        'message'     => 'Payout request submitted. Jared will process within 2 business days.',
        'amount'      => round( $amount, 2 ),
        'paypal_email' => $paypal_email,
    ] );
}

// ─────────────────────────────────────────────────────────────
// GET /payout-history?referral_code=XXXXXXXX  (Phase 3)
// Returns payout requests from the shared JSONL for this code.
// ─────────────────────────────────────────────────────────────
function pb_referral_api_payout_history_wp( WP_REST_Request $request ) {
    $referral_code = sanitize_text_field( $request->get_param( 'referral_code' ) );

    if ( empty( $referral_code ) || ! preg_match( '/^[A-Za-z0-9]{6,12}$/', $referral_code ) ) {
        return new WP_Error( 'invalid_input', 'Valid referral_code required.', [ 'status' => 400 ] );
    }

    $all_requests  = pb_referral_read_payout_requests();
    $user_requests = array_values( array_filter( $all_requests, function( $r ) use ( $referral_code ) {
        return isset( $r['referral_code'] ) && $r['referral_code'] === $referral_code;
    } ) );

    // Sort most recent first
    usort( $user_requests, function( $a, $b ) {
        return (float) ( $b['created_at_ts'] ?? 0 ) <=> (float) ( $a['created_at_ts'] ?? 0 );
    } );

    // Strip null paid_at for clean JSON
    $user_requests = array_map( function( $r ) {
        return [
            'request_id'    => $r['request_id']   ?? '',
            'amount'        => (float) ( $r['amount'] ?? 0 ),
            'paypal_email'  => $r['paypal_email']  ?? '',
            'status'        => $r['status']        ?? 'pending',
            'created_at'    => $r['created_at']    ?? '',
            'paid_at'       => $r['paid_at']       ?? null,
            'notes'         => $r['notes']         ?? '',
        ];
    }, $user_requests );

    return rest_ensure_response( [
        'referral_code' => $referral_code,
        'requests'      => $user_requests,
        'count'         => count( $user_requests ),
    ] );
}

// ============================================================
// PAYOUT JSONL HELPERS
// ============================================================

/**
 * Read all payout requests from the shared JSONL file.
 *
 * @return array
 */
function pb_referral_read_payout_requests() {
    $file = PB_PAYOUT_REQUESTS_FILE;
    if ( ! file_exists( $file ) ) {
        return [];
    }

    $requests = [];
    $lines    = file( $file, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES );
    if ( ! $lines ) {
        return [];
    }

    foreach ( $lines as $line ) {
        $decoded = json_decode( trim( $line ), true );
        if ( is_array( $decoded ) ) {
            $requests[] = $decoded;
        }
    }

    return $requests;
}

/**
 * Append a payout request entry to the shared JSONL file.
 *
 * @param array $entry
 * @return bool
 */
function pb_referral_write_payout_request( array $entry ) {
    $file = PB_PAYOUT_REQUESTS_FILE;
    $dir  = dirname( $file );

    if ( ! is_dir( $dir ) ) {
        // Shared portal directory must already exist — do not auto-create
        return false;
    }

    $line = wp_json_encode( $entry );
    if ( $line === false ) {
        return false;
    }

    $result = file_put_contents( $file, $line . "\n", FILE_APPEND | LOCK_EX );
    return $result !== false;
}

/**
 * Send a Telegram notification via tg_send.sh.
 * Fire-and-forget: uses exec() in background so it doesn't block the request.
 *
 * @param string $message
 */
function pb_referral_tg_notify( $message ) {
    $tg_send = PB_TG_SEND_SH;

    if ( ! file_exists( $tg_send ) || ! is_executable( $tg_send ) ) {
        return;
    }

    // Escape message for shell. Use base64 to avoid quoting issues.
    $encoded = base64_encode( $message );
    $cmd = sprintf(
        'bash -c "echo %s | base64 -d | %s" > /dev/null 2>&1 &',
        escapeshellarg( $encoded ),
        escapeshellarg( $tg_send )
    );

    // tg_send.sh reads from stdin when no args given — but it expects a message arg.
    // Use direct curl fallback instead for reliability.
    pb_referral_tg_notify_curl( $message );
}

/**
 * Notify Jared via direct Telegram API call (wp_remote_post, non-blocking).
 *
 * @param string $message
 */
function pb_referral_tg_notify_curl( $message ) {
    // Read token from tg config
    $config_file = '/home/jared/projects/AI-CIV/aether/config/telegram_config.json';
    if ( ! file_exists( $config_file ) ) {
        return;
    }

    $config = json_decode( file_get_contents( $config_file ), true );
    $token  = $config['bot_token'] ?? '';
    if ( empty( $token ) ) {
        return;
    }

    $chat_id = '548906264';

    wp_remote_post(
        "https://api.telegram.org/bot{$token}/sendMessage",
        [
            'timeout'   => 5,
            'blocking'  => false,   // fire and forget — don't block PHP response
            'body'      => [
                'chat_id' => $chat_id,
                'text'    => $message,
            ],
        ]
    );
}

// ============================================================
// HELPERS
// ============================================================
function pb_referral_generate_code( $length = 8 ) {
    global $wpdb;
    $table = $wpdb->prefix . PB_REFERRAL_USERS_TABLE;
    $chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
    do {
        $code = '';
        for ( $i = 0; $i < $length; $i++ ) {
            $code .= $chars[ random_int( 0, strlen( $chars ) - 1 ) ];
        }
        $exists = $wpdb->get_var( $wpdb->prepare( "SELECT id FROM {$table} WHERE referral_code = %s", $code ) );
    } while ( $exists );
    return $code;
}

function pb_referral_mask_email( $email ) {
    $parts  = explode( '@', $email );
    $local  = $parts[0];
    $domain = $parts[1] ?? 'example.com';
    $masked = substr( $local, 0, 1 ) . str_repeat( '*', max( 1, strlen( $local ) - 1 ) );
    return $masked . '@' . $domain;
}

/**
 * Send email notification to referrer when a conversion fires.
 * Uses WordPress wp_mail — no external mailer required.
 */
function pb_referral_notify_referrer_conversion( $referrer, $referred_masked_email, $referred_name, $earnings, $milestone_bonus ) {
    $to      = $referrer->email;
    $subject = 'You earned $' . number_format( $earnings, 2 ) . ' — Someone joined PureBrain through your link!';

    $bonus_line = $milestone_bonus
        ? "\n\nCONGRATULATIONS! You've hit " . PB_REFERRAL_BONUS_THRESHOLD . " referrals and earned a $" . number_format( PB_REFERRAL_BONUS_AMOUNT, 2 ) . " milestone bonus!"
        : '';

    $message = "Hi {$referrer->name},\n\n"
        . "{$referred_name} ({$referred_masked_email}) just joined PureBrain through your referral link."
        . $bonus_line
        . "\n\nYou earned: \${$earnings}"
        . "\n\nView your full dashboard: " . home_url( '/referral-dashboard/?code=' . $referrer->referral_code )
        . "\n\n— The PureBrain Team";

    $headers = [
        'Content-Type: text/plain; charset=UTF-8',
        'From: PureBrain <purebrain@puremarketing.ai>',
        'Reply-To: jared@puretechnology.nyc',
    ];

    wp_mail( $to, $subject, $message, $headers );
}

// ============================================================
// CORS — allow dashboard page and portal to call the API
// ============================================================
add_action( 'rest_api_init', function() {
    remove_filter( 'rest_pre_serve_request', 'rest_send_cors_headers' );
    add_filter( 'rest_pre_serve_request', function( $value ) {
        $origin  = $_SERVER['HTTP_ORIGIN'] ?? '';
        $allowed = [
            'https://purebrain.ai',
            'https://app.purebrain.ai',
        ];
        if ( in_array( $origin, $allowed, true ) ) {
            header( 'Access-Control-Allow-Origin: ' . esc_url_raw( $origin ) );
        } elseif ( empty( $origin ) ) {
            header( 'Access-Control-Allow-Origin: https://purebrain.ai' );
        }
        header( 'Access-Control-Allow-Methods: GET, POST, OPTIONS' );
        header( 'Access-Control-Allow-Headers: Content-Type, Authorization, X-WP-Nonce' );
        header( 'Access-Control-Allow-Credentials: true' );
        return $value;
    } );
}, 15 );
