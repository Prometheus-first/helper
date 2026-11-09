#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
struct Buffer {uint64_t reserved;void *text;};
__attribute__((naked,noinline,visibility("default"))) uintptr_t fixture_hook_target(void*a,void*b,void*c){
 __asm__ volatile("push %rbp\n push %r15\n push %r14\n push %r13\n push %r12\n push %rbx\n sub $0x48,%rsp\n mov $7,%eax\n add $0x48,%rsp\n pop %rbx\n pop %r12\n pop %r13\n pop %r14\n pop %r15\n pop %rbp\n ret\n");
}
static void make(unsigned char out[24],const char*s){size_t n=strlen(s);memset(out,0,24);if(n<=22){out[0]=(unsigned char)(n*2);memcpy(out+1,s,n);}else{out[0]=1;uint64_t q=n;memcpy(out+8,&q,8);memcpy(out+16,&s,8);}}
static void emit(uint32_t type,const char*body,uint64_t id){
 unsigned char msg[0x70]={0},from[24],to[24],text[24];make(from,type==37?"fmessage":"123456789@chatroom");make(to,"wxid_self_fixture");make(text,body);
 struct Buffer a={0,from},b={0,to},c={0,text};void*pa=&a,*pb=&b,*pc=&c;
 memcpy(msg+8,&pa,8);memcpy(msg+20,&type,4);memcpy(msg+24,&pb,8);memcpy(msg+32,&pc,8);memcpy(msg+0x50,&id,8);if(id)msg[0x6d]|=8;
 fixture_hook_target(0,msg,0);
}
#ifdef HELPER_FIXTURE_VOICE
static void emit_voice(uint32_t declared){
 unsigned char msg[0x70]={0},from[24],to[24],text[24],data[24]={0};
 const unsigned char silk[]={2,'#','!','S','I','L','K','_','V','3',3,0,'a',0,'b'};
 make(from,"123456789@chatroom");make(to,"wxid_self_fixture");
 make(text,"wxid_friend_fixture:\n<msg><voicemsg voicelength='20' length='15'/></msg>");
 data[0]=sizeof(silk)*2;memcpy(data+1,silk,sizeof(silk));
 struct Buffer a={0,from},b={0,to},c={0,text};
 struct {uint64_t reserved;void*data;uint32_t size;uint32_t pad;}audio={0,data,declared,0};
 void *pa=&a,*pb=&b,*pc=&c,*pd=&audio;uint32_t type=34;uint64_t id=100+declared;
 memcpy(msg+8,&pa,8);memcpy(msg+20,&type,4);memcpy(msg+24,&pb,8);memcpy(msg+32,&pc,8);
 memcpy(msg+0x30,&pd,8);memcpy(msg+0x50,&id,8);msg[0x6d]|=8;
 fixture_hook_target(0,msg,0);
 memset(data,0,sizeof(data));
}
#endif
int main(void){usleep(300000);
#ifdef HELPER_FIXTURE_VOICE
 emit_voice(15);emit_voice(16);emit_voice(262145);
 emit(1,"wxid_friend_fixture:\nfixture after voice",900);
#else
 emit(1,"wxid_friend_fixture:\nfixture text",18446744073709551614ULL);
 emit(47,"wxid_friend_fixture:\n<msg><emoji cdnurl='fixture'/></msg>",77);
 emit(43,"wxid_friend_fixture:\n<msg><videomsg length='123' playlength='2'/></msg>",78);
 emit(10002,"<sysmsg type='revokemsg'><revokemsg><session>123456789@chatroom</session><newmsgid>18446744073709551614</newmsgid></revokemsg></sysmsg>",0);
 emit(10000,"\"fixture newcomer\"加入了群聊",80);
 emit(10002,"<sysmsg type='sysmsgtemplate'><sysmsgtemplate><content_template><template>$names$加入了群聊</template><link_list><link name='names'><memberlist><member><username>wxid_new_fixture</username><nickname>fixture newcomer</nickname></member></memberlist></link></link_list></content_template></sysmsgtemplate></sysmsg>",81);
 char *large=malloc(16386);if(!large)return 15;memset(large,'x',16385);large[16385]=0;
 emit(10000,large,0);emit(37,large,0);free(large);
 emit(37,"<msg opcode=\"2\" ticket=\"v4_FIXTURE\"/>",83);
 emit(1,"wxid_friend_fixture:\nfixture after oversized system notice",82);
#endif
 usleep(1200000);return 0;}
