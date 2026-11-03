#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
struct Buffer {uint64_t reserved;void *text;};
struct Msg {unsigned char vtable[8];void *sender;uint32_t flags;uint32_t type;void *receiver;void *content;unsigned char tail[72];};
__attribute__((naked,noinline,visibility("default"))) uintptr_t fixture_hook_target(void*a,void*b,void*c){
 __asm__ volatile("push %rbp\n push %r15\n push %r14\n push %r13\n push %r12\n push %rbx\n sub $0x48,%rsp\n mov $7,%eax\n add $0x48,%rsp\n pop %rbx\n pop %r12\n pop %r13\n pop %r14\n pop %r15\n pop %rbp\n ret\n");
}
static void cxx(unsigned char out[24],const char*s){size_t n=strlen(s);if(n<23){out[0]=(unsigned char)(n*2);memcpy(out+1,s,n);}else{out[0]=1;uint64_t z=n;memcpy(out+8,&z,8);memcpy(out+16,&s,8);}}
#ifdef HELPER_FIXTURE_ROUNDTRIP
#define FRIEND_TEXT "WX_LINUX_BRIDGE_07_SEND"
#define GROUP_TEXT "wxid_friend_fixture:\nWX_LINUX_BRIDGE_08_SEND"
#else
#define FRIEND_TEXT "WX_LINUX_BRIDGE_07"
#define GROUP_TEXT "wxid_friend_fixture:\nWX_LINUX_BRIDGE_08"
#endif
int main(void){
 unsigned char from[24]={0},to[24]={0},body[24]={0},room[24]={0},groupbody[24]={0};
 cxx(from,"wxid_friend_fixture");cxx(to,"wxid_self_fixture");cxx(body,FRIEND_TEXT);cxx(room,"123456789@chatroom");cxx(groupbody,GROUP_TEXT);
 struct Buffer a={0,from},b={0,to},c={0,body},r={0,room},g={0,groupbody};struct Msg m={.sender=&a,.type=1,.receiver=&b,.content=&c};
 usleep(400000);if(fixture_hook_target(NULL,&m,NULL)!=7)return 1;usleep(300000);m.sender=&r;m.content=&g;if(fixture_hook_target(NULL,&m,NULL)!=7)return 2;usleep(600000);return 0;
}
