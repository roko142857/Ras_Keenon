// One SDK owner. Blocking SDK arm/waist calls cannot be preempted by this protocol.
#include "qlrobotarm_canfd.h"
#include "head_feedback.hpp"
#include <algorithm>
#include <array>
#include <cstddef>
#include <chrono>
#include <csignal>
#include <fcntl.h>
#include <poll.h>
#include <sstream>
#include <string>
#include <sys/file.h>
#include <thread>
#include <unistd.h>

extern std::atomic<bool> need_query_h;
// The ABI and interception are reused from the already deployed phase1 bridge.
typedef struct { int type; int command; int32_t data; uint32_t id;
                 int16_t current; float speed; float position; float position_de; } hal_data_recv_t;
static_assert(sizeof(hal_data_recv_t) == 32, "SDK receive ABI mismatch");
static_assert(offsetof(hal_data_recv_t, id) == 12, "SDK id offset mismatch");
static_assert(offsetof(hal_data_recv_t, position_de) == 28, "SDK position offset mismatch");
typedef void (*Callback)(hal_data_recv_t*, int);
extern "C" int16_t real_receive(Callback) asm("__real__Z11hal_receivePFvP15hal_data_recv_tiE");
extern "C" int16_t wrap_receive(Callback) asm("__wrap__Z11hal_receivePFvP15hal_data_recv_tiE");
static Callback vendor_callback = nullptr;
static Feedback head_feedback;
static std::mutex arm_feedback_lock;
static std::array<double,14> arm_time{};
static std::array<uint64_t,14> arm_sequence{};
static double monotonic_s() {
  return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
static void observed_receive(hal_data_recv_t* records, int channel) {
  if (vendor_callback) vendor_callback(records, channel);
  if (!records || channel < 0 || channel >= 5) return;
  const auto& r = records[channel];
  if (r.type == 1 && (r.id == 30 || r.id == 31))
    head_feedback.update(r.id, getJointAngle(r.id), r.speed, monotonic_s());
  // IDs verified against getJointAngle_r/l in the pinned archive: 16..22 / 23..29.
  if (r.type == 1 && r.id >= 16 && r.id <= 29 && std::isfinite(r.position)) {
    std::lock_guard<std::mutex> guard(arm_feedback_lock);
    arm_time[r.id-16] = monotonic_s(); ++arm_sequence[r.id-16];
  }
}
extern "C" int16_t wrap_receive(Callback callback) {
  vendor_callback = callback;
  return real_receive(observed_receive);
}

namespace {
int protocol_fd = -1;
volatile std::sig_atomic_t stopping = 0;
void signal_stop(int) { stopping = 1; }
void reply(unsigned long long id, const std::string& fields) {
  const std::string line = "@RAS {\"id\":" + std::to_string(id) + "," + fields + "}\n";
  size_t at = 0;
  while (at < line.size()) {
    const ssize_t n = write(protocol_fd, line.data() + at, line.size() - at);
    if (n <= 0) { stopping = 1; break; }
    at += static_cast<size_t>(n);
  }
}
void error(unsigned long long id, const char* message, bool refused=true) {
  reply(id, std::string("\"ok\":false,\"refused\":") + (refused ? "true" : "false")
        + ",\"error\":\"" + message + "\"");
}
bool finite(double x) { return std::isfinite(x); }
// Pinned vendor API is asymmetric: getters return degrees; moveJ_* targets are radians.
std::vector<double> radians(std::vector<double> degrees) {
  for (double& value : degrees) value *= std::acos(-1.)/180.;
  return degrees;
}
std::string array(const std::vector<double>& v) {
  std::ostringstream out; out.precision(12); out << '[';
  for (size_t i = 0; i < v.size(); ++i) {
    if (i) out << ',';
    if (finite(v[i])) out << v[i]; else out << "null";
  }
  out << ']'; return out.str();
}
bool extra(std::istringstream& in) { std::string v; return bool(in >> v); }
std::string arm_status(int first) {
  std::lock_guard<std::mutex> guard(arm_feedback_lock);
  std::ostringstream out; out.precision(12);
  const double now = monotonic_s(); bool fresh = true;
  out << "{\"joint_ids\":[";
  for (int i=0;i<7;++i) { if(i)out<<',';out<<first+i; }
  out << "],\"feedback_age_s\":[";
  for (int i=0;i<7;++i) {
    if(i)out<<',';
    const int at=first+i-16;
    if(arm_sequence[at])out<<now-arm_time[at];else out<<"null";
    fresh = fresh && arm_sequence[at] && now-arm_time[at]<=1.;
  }
  out << "],\"feedback_sequence\":[";
  for (int i=0;i<7;++i) { if(i)out<<',';out<<arm_sequence[first+i-16]; }
  out << "],\"feedback_fresh\":" << (fresh?"true":"false")
      << ",\"motor_power_on\":null,\"motor_error_codes\":null}";
  return out.str();
}
void command(const std::string& line) {
  std::istringstream in(line); unsigned long long id = 0; std::string op;
  if (!(in >> id >> op)) { error(0, "bad request"); return; }
  if (op == "QUIT") { stopping = 1; reply(id, "\"ok\":true"); return; }
  if (op == "READ") {
    const double yaw = getJointAngle(30), pitch = getJointAngle(31);
    const auto right_deg = getJointAngle_r(), left_deg = getJointAngle_l(), waist_deg = getJointAngle_w();
    if (!finite(yaw) || !finite(pitch)) { error(id, "nonfinite SDK head feedback", false); return; }
    const auto sample = head_feedback.snapshot(monotonic_s());
    std::ostringstream out; out.precision(12);
    out << "\"ok\":true,\"yaw_deg\":" << yaw << ",\"pitch_deg\":" << pitch
        << ",\"right\":" << array(radians(right_deg))
        << ",\"left\":" << array(radians(left_deg))
        << ",\"right_deg\":" << array(right_deg)
        << ",\"left_deg\":" << array(left_deg)
        << ",\"arm_angle_unit\":\"rad\",\"sdk_feedback_angle_unit\":\"deg\""
        << ",\"arm_status\":{\"right\":" << arm_status(16)
        << ",\"left\":" << arm_status(23) << "}"
        << ",\"waist\":" << array(radians(waist_deg))
        << ",\"waist_deg\":" << array(waist_deg) << ",\"waist_angle_unit\":\"rad\""
        << ",\"feedback_kind\":\"head_CSP_callback_arms_SDK_cache\""
        << ",\"head_feedback_fresh\":" << (sample.fresh ? "true" : "false")
        << ",\"head_feedback_age_s\":";
    if (sample.yaw_valid && sample.pitch_valid)
      out << std::max(monotonic_s()-sample.yaw_time, monotonic_s()-sample.pitch_time);
    else out << "null";
    out << ",\"head_yaw_sequence\":" << sample.yaw_seq
        << ",\"head_pitch_sequence\":" << sample.pitch_seq;
    reply(id, out.str()); return;
  }
  if (op == "HOLD") {
    setJointAngle(30, getJointAngle(30)); setJointAngle(31, getJointAngle(31));
    reply(id, "\"ok\":true"); return;
  }
  if (op == "HEAD") {
    double yaw, pitch;
    if (!(in >> yaw >> pitch) || extra(in) || !finite(yaw) || !finite(pitch)
        || std::abs(yaw) > 20 || std::abs(pitch) > 20) {
      error(id, "head range"); return;
    }
    setJointAngle(30, yaw); setJointAngle(31, pitch);
    reply(id, "\"ok\":true,\"arrival_verified\":false"); return;
  }
  if (op == "WAIST") {
    double height;
    int direction = -1;
    bool parsed = bool(in >> height);
    in >> std::ws;
    if (in.peek() != EOF) parsed = bool(in >> direction) && parsed;
    if (!parsed || extra(in) || !finite(height) || height < 0 || height > .273
        || (direction != -1 && direction != 1)) {
      error(id, "waist height outside 0..0.273 m or invalid direction"); return;
    }
    const double theta = std::acos(1-height/(.392+.374));
    const int code = moveJ_w({0., direction*theta, direction*2*theta, direction*theta, 0., 0.});
    if (code != 0) { error(id, "waist SDK returned failure", false); return; }
    reply(id, "\"ok\":true,\"arrival_verified\":false"); return;
  }
  if (op == "ARM") {
    std::string side; double speed = 0.; std::vector<double> q(7);
    bool valid = bool(in >> side) && (side == "left" || side == "right");
    for (double& v : q) valid = bool(in >> v) && finite(v) && std::abs(v) <= 3.2 && valid;
    valid = bool(in >> speed) && finite(speed) && speed >= .1 && speed <= 1. && valid;
    if (!valid || extra(in)) { error(id, "invalid arm request"); return; }
    const int code = side == "left" ? moveJ_l_block(q, speed) : moveJ_r_block(q, speed);
    const std::string fields = "\"sdk_return\":"+std::to_string(code)+",\"side\":\""+side+"\"";
    if (code != 1) {
      reply(id, "\"ok\":false,\"refused\":false,"+fields+
        ",\"error\":\""+side+" arm SDK returned code "+std::to_string(code)+"; inspect arm_status and controller log\"");
      return;
    }
    reply(id, "\"ok\":true,\"arrival_verified\":false,"+fields); return;
  }
  error(id, "unknown operation");
}
}

int main(int argc, char** argv) {
  if (argc != 2 || std::string(argv[1]) != "--hardware") {
    std::cerr << "sdk_bridge --hardware: INITIALIZES ARMS/HEAD/WAIST. Use ROS mock backend for tests.\n";
    return 2;
  }
  protocol_fd = dup(STDOUT_FILENO);
  if (protocol_fd < 0 || dup2(STDERR_FILENO, STDOUT_FILENO) < 0) return 2;
  signal(SIGINT, signal_stop); signal(SIGTERM, signal_stop); signal(SIGPIPE, SIG_IGN);
  // This pinned archive uses 1=both, 2=right, 3=left (its header comment is stale).
  robot_init(1);
  need_query_h = true;
  std::this_thread::sleep_for(std::chrono::milliseconds(500));
  reply(0, "\"ok\":true");
  std::string pending; char buffer[1024];
  while (!stopping) {
    pollfd item{STDIN_FILENO, POLLIN, 0};
    const int ready = poll(&item, 1, 100);
    if (ready < 0) continue;
    if (!ready) continue;
    const ssize_t n = read(STDIN_FILENO, buffer, sizeof buffer);
    if (n <= 0) break;
    pending.append(buffer, size_t(n));
    if (pending.size() > 4096) { error(0, "input too large"); break; }
    size_t end;
    while (!stopping && (end = pending.find('\n')) != std::string::npos) {
      const auto line = pending.substr(0, end); pending.erase(0, end+1); command(line);
    }
  }
  // No asynchronous stop claim: an in-flight blocking call must finish first.
  stopRightArm(); robot_end();
  close(protocol_fd); return 0;
}
