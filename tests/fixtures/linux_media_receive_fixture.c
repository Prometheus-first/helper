#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
struct Buffer {uint64_t reserved;void *text;};
struct Msg {unsigned char vtable[8];void *sender;uint32_t flags;uint32_t type;void *receiver;void *content;unsigned char tail[72];};
__attribute__((naked,noinline,visibility("default"))) uintptr_t fixture_hook_target(void*a,void*b,void*c){
 __asm__ volatile("push %rbp\n push %r15\n push %r14\n push %r13\n push %r12\n push %rbx\n sub $0x48,%rsp\n mov $7,%eax\n add $0x48,%rsp\n pop %rbx\n pop %r12\n pop %r13\n pop %r14\n pop %r15\n pop %rbp\n ret\n");
}
static void cxx(unsigned char out[24],const char*s){size_t n=strlen(s);if(n<23){out[0]=(unsigned char)(n*2);memcpy(out+1,s,n);}else{out[0]=1;uint64_t z=n;memcpy(out+8,&z,8);memcpy(out+16,&s,8);}}
static void emit(int type,const char*peer,const char*text){
 const char *override=getenv("HELPER_FIXTURE_MEDIA_PEER");if(override&&!strcmp(peer,"wxid_friend_fixture"))peer=override;
 unsigned char from[24]={0},to[24]={0},body[24]={0};cxx(from,peer);cxx(to,"wxid_self_fixture");cxx(body,text);
 struct Buffer a={0,from},b={0,to},c={0,body};struct Msg m={.sender=&a,.type=(uint32_t)type,.receiver=&b,.content=&c};
 if(fixture_hook_target(NULL,&m,NULL)!=7)_exit(10);usleep(60000);
}
int main(void){
 usleep(200000);
 if(getenv("HELPER_FIXTURE_MEDIA_PEER")){
 emit(1,"filehelper","WX_LINUX_MEDIA_ARM_11");
 emit(1,"gh_fixture","WX_LINUX_MEDIA_ARM_11");
 emit(1,"123@chatroom","WX_LINUX_MEDIA_ARM_11");
 emit(1,"bad peer","WX_LINUX_MEDIA_ARM_11");
 }
 emit(3,"wxid_friend_fixture","<msg><img filekey=\"fixture-before-arm\"/></msg>");
 emit(1,"wxid_friend_fixture","WX_LINUX_MEDIA_ARM_11");
 emit(3,"wxid_other_fixture","<msg><img filekey=\"wrong-peer\"/></msg>");
 emit(3,"wxid_friend_fixture","<msg><img filekey=\"fixture-image\" aeskey=\"fixture-key\"/></msg>");
 emit(3,"wxid_friend_fixture","<msg><img filekey=\"quota-image\"/></msg>");
 emit(49,"wxid_other_fixture","<msg><appmsg><title>wrong-peer.txt</title></appmsg></msg>");
 emit(49,"wxid_friend_fixture","<msg><appmsg><title>WX_LINUX_FILE_RECV_01.txt</title><type>6</type></appmsg></msg>");
 emit(49,"wxid_friend_fixture","<msg><appmsg><title>quota-file.txt</title></appmsg></msg>");
 emit(34,"wxid_other_fixture","<msg><voicemsg voicelength=\"2000\"/></msg>");
 emit(34,"wxid_friend_fixture","<msg><voicemsg voicelength=\"2000\" length=\"4096\" bufid=\"private-fixture\"/></msg>");
 emit(34,"wxid_friend_fixture","<msg><voicemsg voicelength=\"3000\"/></msg>");
 emit(1,"wxid_other_fixture","WX_LINUX_MEDIA_ARM_11");
 emit(3,"wxid_other_fixture","<msg><img filekey=\"takeover-rejected\"/></msg>");
 emit(1,"wxid_friend_fixture","WX_LINUX_MEDIA_ARM_11");
 emit(3,"wxid_friend_fixture","<msg><img filekey=\"fixture-image-second-round\"/></msg>");
 usleep(500000);return 0;
}
