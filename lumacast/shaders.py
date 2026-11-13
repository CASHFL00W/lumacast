"""GLSL-Shader für Farbe, Skalierung, Schärfen und Overlay."""

VERTEX = """
#version 330
in vec2 in_pos;
out vec2 v_uv;
uniform vec4 uv_rect;   // xy = Offset, zw = Größe (für Zuschneiden)
void main() {
    vec2 uv = in_pos * 0.5 + 0.5;
    v_uv = uv_rect.xy + uv * uv_rect.zw;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

# Pass 1: Farbe/Bildanpassung in Quellauflösung
COLOR = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D src;
uniform bool bgr;
uniform bool limited_range;
uniform int mode;          // 0 original, 1 manuell, 2 auto, 3 hybrid
uniform float compare_split;   // <0 = aus, sonst x-Position (0..1) der Trennlinie
// manuell
uniform float brightness;  // -0.25..0.25
uniform float contrast;    // Faktor
uniform float saturation;  // Faktor
uniform float vibrance;    // -1..1
uniform float gamma_exp;   // Exponent
uniform vec3 tint;         // Farbtemperatur
uniform float black;       // Schwarzpunkt
// auto
uniform float auto_black;
uniform float auto_white;
uniform float auto_gamma;

const vec3 LUMA = vec3(0.2126, 0.7152, 0.0722);

vec3 apply_auto(vec3 c) {
    c = (c - auto_black) / max(auto_white - auto_black, 0.05);
    c = clamp(c, 0.0, 1.0);
    return pow(c, vec3(auto_gamma));
}

vec3 apply_manual(vec3 c) {
    c = (c - black) / max(1.0 - black, 0.05);
    c = c + brightness;
    c = (c - 0.5) * contrast + 0.5;
    c = clamp(c, 0.0, 1.0);
    c = pow(c, vec3(gamma_exp));
    float l = dot(c, LUMA);
    c = mix(vec3(l), c, saturation);
    float mx = max(c.r, max(c.g, c.b));
    float mn = min(c.r, min(c.g, c.b));
    float sat = mx - mn;
    c = mix(vec3(l), c, 1.0 + vibrance * (1.0 - sat));
    c *= tint;
    return clamp(c, 0.0, 1.0);
}

void main() {
    // Kamerabild hat Zeile 0 oben, OpenGL unten -> hier einmal spiegeln
    vec3 c = texture(src, vec2(v_uv.x, 1.0 - v_uv.y)).rgb;
    if (bgr) c = c.bgr;
    if (limited_range) c = clamp((c - 16.0/255.0) * (255.0/219.0), 0.0, 1.0);
    bool original_side = compare_split >= 0.0 && v_uv.x < compare_split;
    if (!original_side) {
        if (mode == 2 || mode == 3) c = apply_auto(c);
        if (mode == 1 || mode == 3) c = apply_manual(c);
    }
    f_color = vec4(c, 1.0);
}
"""

# Pass 2: Hochskalieren (nearest / bilinear / bicubic / lanczos)
UPSCALE = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D src;
uniform vec2 src_size;
uniform int method;        // 0 nearest, 1 bilinear, 2 bicubic, 3 lanczos
uniform bool anti_ringing;

vec3 fetch(ivec2 p) {
    p = clamp(p, ivec2(0), ivec2(src_size) - 1);
    return texelFetch(src, p, 0).rgb;
}

float cubic(float x) {          // Catmull-Rom
    x = abs(x);
    if (x < 1.0) return 1.5*x*x*x - 2.5*x*x + 1.0;
    if (x < 2.0) return -0.5*x*x*x + 2.5*x*x - 4.0*x + 2.0;
    return 0.0;
}

const float PI = 3.14159265359;
float lanczos(float x) {
    x = abs(x);
    if (x < 1e-5) return 1.0;
    if (x >= 3.0) return 0.0;
    float px = PI * x;
    return 3.0 * sin(px) * sin(px / 3.0) / (px * px);
}

void main() {
    vec2 pos = v_uv * src_size - 0.5;
    if (method == 0) {
        f_color = vec4(fetch(ivec2(floor(v_uv * src_size))), 1.0);
        return;
    }
    if (method == 1) {
        f_color = vec4(texture(src, v_uv).rgb, 1.0);
        return;
    }
    ivec2 base = ivec2(floor(pos));
    vec2 f = pos - vec2(base);
    vec3 sum = vec3(0.0);
    float wsum = 0.0;
    if (method == 2) {
        for (int y = -1; y <= 2; y++) {
            float wy = cubic(float(y) - f.y);
            for (int x = -1; x <= 2; x++) {
                float w = cubic(float(x) - f.x) * wy;
                sum += fetch(base + ivec2(x, y)) * w;
                wsum += w;
            }
        }
    } else {
        for (int y = -2; y <= 3; y++) {
            float wy = lanczos(float(y) - f.y);
            for (int x = -2; x <= 3; x++) {
                float w = lanczos(float(x) - f.x) * wy;
                sum += fetch(base + ivec2(x, y)) * w;
                wsum += w;
            }
        }
    }
    vec3 c = sum / wsum;
    if (anti_ringing) {
        vec3 a = fetch(base);
        vec3 b = fetch(base + ivec2(1, 0));
        vec3 d = fetch(base + ivec2(0, 1));
        vec3 e = fetch(base + ivec2(1, 1));
        vec3 mn = min(min(a, b), min(d, e));
        vec3 mx = max(max(a, b), max(d, e));
        c = clamp(c, mn, mx);
    }
    f_color = vec4(clamp(c, 0.0, 1.0), 1.0);
}
"""

# Pass 3: Kontrastadaptives Schärfen (nach AMD CAS, MIT-Lizenz)
SHARPEN = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D src;
uniform float sharpness;   // 0..1
uniform float compare_split;
uniform vec4 dest_rect;    // x, y, w, h in Pixeln (Bildbereich)

vec3 px(ivec2 p) { return texelFetch(src, p, 0).rgb; }

void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    vec3 e = px(p);
    float rel_x = (gl_FragCoord.x - dest_rect.x) / dest_rect.z;
    if (compare_split >= 0.0 && rel_x < compare_split) {
        f_color = vec4(e, 1.0);
        return;
    }
    vec3 a = px(p + ivec2(-1, -1)), b = px(p + ivec2(0, -1)), c = px(p + ivec2(1, -1));
    vec3 d = px(p + ivec2(-1, 0)),                              f = px(p + ivec2(1, 0));
    vec3 g = px(p + ivec2(-1, 1)), h = px(p + ivec2(0, 1)),  i = px(p + ivec2(1, 1));

    vec3 mn = min(min(min(d, e), min(f, b)), h);
    vec3 mn2 = min(mn, min(min(a, c), min(g, i)));
    mn += mn2;
    vec3 mx = max(max(max(d, e), max(f, b)), h);
    vec3 mx2 = max(mx, max(max(a, c), max(g, i)));
    mx += mx2;

    vec3 amp = clamp(min(mn, 2.0 - mx) / max(mx, vec3(1e-5)), 0.0, 1.0);
    amp = sqrt(amp);
    float peak = -1.0 / mix(8.0, 5.0, sharpness);
    vec3 w = amp * peak;
    vec3 rcp = 1.0 / (1.0 + 4.0 * w);
    vec3 outc = ((b + d + f + h) * w + e) * rcp;
    f_color = vec4(clamp(outc, 0.0, 1.0), 1.0);
}
"""

# Overlay (Text-Textur mit Alpha) und einfarbige Flächen
OVERLAY = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D src;
uniform bool use_tex;
uniform vec4 color;
void main() {
    if (use_tex) {
        vec4 t = texture(src, vec2(v_uv.x, 1.0 - v_uv.y));
        f_color = t;
    } else {
        f_color = color;
    }
}
"""
