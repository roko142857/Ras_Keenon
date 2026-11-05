#pragma once
#include <cmath>
#include <cstdint>
#include <mutex>
struct HeadSample {
    double yaw=0, pitch=0, yaw_speed=0, pitch_speed=0, yaw_time=-1, pitch_time=-1;
    uint64_t yaw_seq=0, pitch_seq=0;
    bool yaw_valid=false, pitch_valid=false, fresh=false;
};
class Feedback {
    std::mutex lock;
    HeadSample value;
public:
    void update(int id, double degrees, double speed, double received) {
        if(id!=30 && id!=31) return;
        std::lock_guard<std::mutex> guard(lock);
        bool valid=std::isfinite(degrees) && std::abs(degrees)<=180 &&
                   std::isfinite(speed) && std::isfinite(received);
        if(id==30) {
            value.yaw_valid=valid;
            if(valid) { value.yaw=degrees; value.yaw_speed=speed; value.yaw_time=received; ++value.yaw_seq; }
        } else {
            value.pitch_valid=valid;
            if(valid) { value.pitch=degrees; value.pitch_speed=speed; value.pitch_time=received; ++value.pitch_seq; }
        }
    }
    HeadSample snapshot(double now) {
        std::lock_guard<std::mutex> guard(lock);
        HeadSample s=value;
        s.fresh=s.yaw_valid && s.pitch_valid && s.yaw_time>=0 && s.pitch_time>=0 && now>=s.yaw_time && now>=s.pitch_time &&
                now-s.yaw_time<=.25 && now-s.pitch_time<=.25;
        return s;
    }
};
